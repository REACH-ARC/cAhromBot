"""Retroactive outcome scorer for shadow-mode signal logs.

Reads ``signals.log``, parses each BUY/SELL entry's stop_loss and
take_profit levels, fetches M15 candles for the configured symbol,
and walks forward from each signal's timestamp to determine whether
TP or SL was touched first. Writes one JSONL record per scored
signal to ``signal_outcomes.log`` and prints a summary distribution
broken down by confidence bucket.

Conventions:
- Pessimistic same-candle resolution: if both TP and SL fall inside
  the same candle's range, SL is treated as hit first. This matches
  standard backtest practice and avoids overstating hit rate.
- Max horizon: 24 hours of M15 candles after signal time. Beyond that
  the signal is reported as ``expired_neutral``.
- Idempotent: re-running rebuilds ``signal_outcomes.log`` from scratch.
"""

from __future__ import annotations

import json
import logging
import re
import sys
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import List, Optional, Tuple

import pandas as pd

logger = logging.getLogger("xaubot.score")

_SIGNALS_PATH = Path(__file__).parent / "signals.log"
_OUTCOMES_PATH = Path(__file__).parent / "signal_outcomes.log"
_HORIZON_HOURS = 24
_MAX_CANDLES_PER_FETCH = 5000


def _parse_price(value: object) -> Optional[float]:
    """Extract a single float from a Claude-formatted price string.

    Accepts plain numbers (``"4602.00"``), ranges (``"4585.00-4592.00"``
    → midpoint), and any of the unicode dashes Claude sometimes emits.

    Args:
        value: Raw value from the signal record.

    Returns:
        Parsed float, or None if no number can be extracted.
    """
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    s = str(value).replace(",", "")
    nums = re.findall(r"-?\d+(?:\.\d+)?", s)
    if not nums:
        return None
    if len(nums) == 1:
        return float(nums[0])
    return (float(nums[0]) + float(nums[1])) / 2.0


def _load_signal_records() -> List[dict]:
    """Load all parseable JSONL records from ``signals.log``.

    Returns:
        List of dicts in file order. Malformed lines are skipped silently.
    """
    if not _SIGNALS_PATH.exists():
        logger.error("No signals.log found at %s", _SIGNALS_PATH)
        return []
    records: List[dict] = []
    with _SIGNALS_PATH.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return records


def _earliest_signal_ts(records: List[dict]) -> Optional[datetime]:
    """Return the earliest signal timestamp in the records, or None.

    Args:
        records: Signal records loaded from ``_load_signal_records``.
    """
    earliest: Optional[datetime] = None
    for r in records:
        try:
            ts = datetime.fromisoformat(r["ts"])
        except (KeyError, ValueError):
            continue
        if earliest is None or ts < earliest:
            earliest = ts
    return earliest


def _fetch_m15_history(symbol: str, since: datetime) -> Optional[pd.DataFrame]:
    """Fetch enough M15 candles to cover all signal timestamps.

    Args:
        symbol: Symbol such as "XAU/USD".
        since: Earliest signal timestamp; we need candles from at
            least this point forward.

    Returns:
        DataFrame indexed by candle ``time`` with columns
        ``open, high, low, close, volume``. Returns ``None`` if the
        fetch fails.
    """
    candles_needed = int(
        (datetime.now(timezone.utc) - since).total_seconds() / 60 / 15
    ) + _HORIZON_HOURS * 4 + 50
    outputsize = max(200, min(candles_needed, _MAX_CANDLES_PER_FETCH))

    from data import MarketDataClient

    client = MarketDataClient()
    df = client.get_candles(symbol, "15min", outputsize=outputsize)
    if df is None:
        return None
    df = df.set_index("time").sort_index()
    return df


def _score_one(
    record: dict,
    candles: pd.DataFrame,
) -> Optional[dict]:
    """Grade one signal record against the M15 candle history.

    Args:
        record: A single signal record loaded from signals.log.
        candles: M15 DataFrame indexed by ``time`` (UTC).

    Returns:
        A flat outcome dict, or ``None`` if the record cannot be graded
        (e.g. WAIT signals or unparseable prices).
    """
    direction = str(record.get("signal", "")).upper()
    if direction not in ("BUY", "SELL"):
        return None

    sl = _parse_price(record.get("stop_loss"))
    tp = _parse_price(record.get("take_profit"))
    entry_price = _parse_price(record.get("price"))
    if sl is None or tp is None or entry_price is None:
        return {
            "signal_ts": record.get("ts"),
            "direction": direction,
            "confidence": record.get("confidence"),
            "delivered": record.get("delivered"),
            "outcome": "error",
            "error": "could not parse price levels",
        }

    try:
        signal_ts = datetime.fromisoformat(record["ts"])
    except (KeyError, ValueError):
        return None

    horizon_end = signal_ts + timedelta(hours=_HORIZON_HOURS)
    forward = candles[(candles.index > signal_ts) & (candles.index <= horizon_end)]
    if forward.empty:
        return {
            "signal_ts": record["ts"],
            "direction": direction,
            "confidence": record.get("confidence"),
            "delivered": record.get("delivered"),
            "entry_price": entry_price,
            "stop_loss": sl,
            "take_profit": tp,
            "outcome": "still_open",
            "outcome_ts": None,
            "candles_to_outcome": 0,
        }

    # Walk forward candle by candle, pessimistic same-candle resolution.
    outcome = "expired_neutral"
    outcome_ts: Optional[datetime] = None
    candles_to_outcome = 0
    for i, (ts, row) in enumerate(forward.iterrows(), start=1):
        high = float(row["high"])
        low = float(row["low"])
        if direction == "SELL":
            sl_hit = high >= sl
            tp_hit = low <= tp
        else:  # BUY
            sl_hit = low <= sl
            tp_hit = high >= tp

        if sl_hit and tp_hit:
            outcome = "SL_hit"  # pessimistic
            outcome_ts = ts
            candles_to_outcome = i
            break
        if sl_hit:
            outcome = "SL_hit"
            outcome_ts = ts
            candles_to_outcome = i
            break
        if tp_hit:
            outcome = "TP_hit"
            outcome_ts = ts
            candles_to_outcome = i
            break

    if outcome == "expired_neutral":
        candles_to_outcome = len(forward)
        if forward.index[-1] >= datetime.now(timezone.utc) - timedelta(minutes=15):
            outcome = "still_open"

    return {
        "signal_ts": record["ts"],
        "direction": direction,
        "confidence": record.get("confidence"),
        "delivered": record.get("delivered"),
        "entry_price": entry_price,
        "stop_loss": sl,
        "take_profit": tp,
        "outcome": outcome,
        "outcome_ts": outcome_ts.isoformat() if outcome_ts else None,
        "candles_to_outcome": candles_to_outcome,
    }


def _summarize(outcomes: List[dict]) -> str:
    """Render a human-readable summary of the scored outcomes.

    Args:
        outcomes: Outcome dicts produced by ``_score_one``.

    Returns:
        Multi-line summary string.
    """
    if not outcomes:
        return "No outcomes to summarize."

    total = len(outcomes)
    by_outcome = Counter(o["outcome"] for o in outcomes)
    delivered = [o for o in outcomes if o.get("delivered")]
    delivered_by_outcome = Counter(o["outcome"] for o in delivered)

    bucket_counts: dict = {}
    for o in outcomes:
        c = o.get("confidence")
        if not isinstance(c, int):
            continue
        bucket = f"{(c // 10) * 10}-{(c // 10) * 10 + 9}"
        bucket_counts.setdefault(bucket, Counter())
        bucket_counts[bucket][o["outcome"]] += 1

    lines = [
        f"Total signals scored: {total}",
        f"Delivered to Telegram: {len(delivered)}",
        "",
        "All signals (incl. filter-blocked):",
    ]
    for k in ("TP_hit", "SL_hit", "expired_neutral", "still_open", "error"):
        if by_outcome[k]:
            lines.append(f"  {k}: {by_outcome[k]}")

    lines.append("")
    lines.append("Delivered signals only:")
    for k in ("TP_hit", "SL_hit", "expired_neutral", "still_open", "error"):
        if delivered_by_outcome[k]:
            lines.append(f"  {k}: {delivered_by_outcome[k]}")

    if delivered:
        resolved = delivered_by_outcome["TP_hit"] + delivered_by_outcome["SL_hit"]
        if resolved:
            hit_rate = delivered_by_outcome["TP_hit"] / resolved
            lines.append(f"  Delivered hit rate (TP / (TP+SL)): {hit_rate:.0%}")

    lines.append("")
    lines.append("By confidence bucket (TP / SL / open / neutral):")
    for bucket in sorted(bucket_counts.keys()):
        c = bucket_counts[bucket]
        resolved = c["TP_hit"] + c["SL_hit"]
        rate = f"{c['TP_hit'] / resolved:.0%}" if resolved else "n/a"
        lines.append(
            f"  {bucket}: TP={c['TP_hit']}  SL={c['SL_hit']}  "
            f"open={c['still_open']}  neutral={c['expired_neutral']}  "
            f"hit_rate={rate}"
        )
    return "\n".join(lines)


def main() -> int:
    """Entry point: load signals, fetch candles, score, write outcomes."""
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")

    from config import SYMBOL

    records = _load_signal_records()
    if not records:
        logger.error("No signal records found; nothing to score")
        return 1

    earliest = _earliest_signal_ts(records)
    if earliest is None:
        logger.error("Could not determine earliest signal timestamp")
        return 1

    logger.info("Loaded %d signal records; earliest=%s", len(records), earliest)
    logger.info("Fetching M15 history for %s", SYMBOL)
    candles = _fetch_m15_history(SYMBOL, earliest)
    if candles is None:
        logger.error("M15 history fetch failed; cannot score")
        return 1
    logger.info("Fetched %d M15 candles (%s -> %s)",
                len(candles), candles.index[0], candles.index[-1])

    outcomes: List[dict] = []
    for r in records:
        scored = _score_one(r, candles)
        if scored is not None:
            outcomes.append(scored)

    with _OUTCOMES_PATH.open("w", encoding="utf-8") as fh:
        for o in outcomes:
            fh.write(json.dumps(o) + "\n")
    logger.info("Wrote %d outcomes to %s", len(outcomes), _OUTCOMES_PATH)

    print()
    print(_summarize(outcomes))
    return 0


if __name__ == "__main__":
    sys.exit(main())
