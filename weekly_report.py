"""Weekly signal-distribution report for shadow-mode review.

Reads the last 7 days of records from ``signals.log``, summarises the
distribution by direction, confidence bucket, and filter outcome, and
sends the result to Telegram via the existing notifier. Designed to be
invoked on a schedule (systemd timer, cron, etc.); writes nothing to
disk and never raises out of ``main()``.
"""

from __future__ import annotations

import json
import logging
import sys
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import List

logger = logging.getLogger("xaubot.weekly_report")

_LOG_PATH = Path(__file__).parent / "signals.log"
_WINDOW_DAYS = 7


def _load_recent(days: int) -> List[dict]:
    """Load JSONL records from the last ``days`` days.

    Args:
        days: Window size in days, anchored to "now" (UTC).

    Returns:
        List of parsed record dicts. Malformed lines are skipped.
    """
    if not _LOG_PATH.exists():
        return []
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    out: List[dict] = []
    with _LOG_PATH.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
                ts = datetime.fromisoformat(rec["ts"])
                if ts >= cutoff:
                    out.append(rec)
            except (json.JSONDecodeError, KeyError, ValueError):
                continue
    return out


def _bucket(confidence: int) -> str:
    """Return a confidence-bucket label like ``"60-69"``.

    Args:
        confidence: Integer confidence score 0-100.
    """
    low = (confidence // 10) * 10
    return f"{low}-{low + 9}"


def _calibration_lines(outcomes: List[dict]) -> List[str]:
    """Build the calibration block for the weekly report.

    Bins delivered (or all resolved) outcomes by confidence decile and
    shows TP / SL counts plus hit rate. This is the single most useful
    piece of feedback for tuning ``CONFIDENCE_MIN`` — if the 60-69
    bucket hits 45% and the 70-79 bucket hits 70%, the user knows the
    threshold is well-placed.

    Args:
        outcomes: Outcome dicts from ``score_records``/``load_recent_outcomes``.

    Returns:
        Lines (each one a row) ready to be joined into the report. Empty
        list when there is nothing to report.
    """
    resolvable = [
        o for o in outcomes
        if o.get("outcome") in ("TP_hit", "SL_hit") and isinstance(o.get("confidence"), int)
    ]
    if not resolvable:
        return []

    bucket_counts: dict = {}
    for o in resolvable:
        b = _bucket(o["confidence"])
        bucket_counts.setdefault(b, Counter())
        bucket_counts[b][o["outcome"]] += 1

    lines = ["", "<b>Calibration (TP / SL / hit rate)</b>"]
    overall_tp = 0
    overall_sl = 0
    for b in sorted(bucket_counts.keys()):
        c = bucket_counts[b]
        tp = c["TP_hit"]
        sl = c["SL_hit"]
        overall_tp += tp
        overall_sl += sl
        total = tp + sl
        rate = f"{tp / total:.0%}" if total else "n/a"
        lines.append(f"  {b}: TP={tp}  SL={sl}  hit_rate={rate}")
    overall_total = overall_tp + overall_sl
    if overall_total:
        lines.append(
            f"  overall: TP={overall_tp}  SL={overall_sl}  "
            f"hit_rate={overall_tp / overall_total:.0%}"
        )
    return lines


def _format_report(records: List[dict], outcomes: List[dict]) -> str:
    """Render the records as a Telegram-friendly summary string.

    Args:
        records: List of signal records from ``_load_recent``.
        outcomes: List of scored outcome dicts (may be empty).
    """
    if not records:
        return (
            f"<b>xaubot weekly report</b>\n"
            f"No signals recorded in the last {_WINDOW_DAYS} days."
        )

    total = len(records)
    by_dir = Counter(r.get("signal", "?") for r in records)
    by_filter = Counter(r.get("filter_reason", "?") for r in records)
    delivered = sum(1 for r in records if r.get("delivered"))

    actionable = [r for r in records if r.get("signal") in ("BUY", "SELL")
                  and isinstance(r.get("confidence"), int)]
    by_bucket: Counter = Counter(_bucket(r["confidence"]) for r in actionable)

    lines = [
        f"<b>xaubot weekly report</b> (last {_WINDOW_DAYS}d)",
        f"Total signals: <b>{total}</b>  |  delivered: <b>{delivered}</b>",
        "",
        "<b>By direction</b>",
        *(f"  {d}: {by_dir[d]}" for d in ("BUY", "SELL", "WAIT") if by_dir[d]),
        "",
        "<b>BUY/SELL by confidence</b>",
        *(f"  {b}: {by_bucket[b]}" for b in sorted(by_bucket.keys())),
        "",
        "<b>Filter outcomes</b>",
        *(f"  {reason}: {count}" for reason, count in by_filter.most_common()),
    ]
    lines.extend(_calibration_lines(outcomes))
    return "\n".join(lines)


def _gather_outcomes(days: int) -> List[dict]:
    """Score recent records, falling back to a pre-computed file.

    Tries to call the scorer inline so the report always has fresh
    calibration data. If the scorer fails (no network, rate limit), it
    falls back to the most recent ``signal_outcomes.log`` content.

    Args:
        days: Window size in days, anchored to "now" (UTC).

    Returns:
        List of outcome dicts, possibly empty.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    try:
        from score_signals import load_recent_outcomes, score_records

        from config import SYMBOL
    except ImportError as exc:  # pragma: no cover - import-time only
        logger.error("Could not import scorer: %s", exc)
        return []

    try:
        outcomes = score_records(SYMBOL, since=cutoff, write_outcomes_file=True)
        if outcomes:
            return outcomes
    except Exception as exc:  # noqa: BLE001 - scoring is best-effort
        logger.warning("Inline scoring failed (%s); falling back to file", exc)

    return load_recent_outcomes(days)


def main() -> int:
    """Build and dispatch the weekly report. Always returns 0/1."""
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")

    try:
        records = _load_recent(_WINDOW_DAYS)
        outcomes = _gather_outcomes(_WINDOW_DAYS)
        report = _format_report(records, outcomes)
    except Exception as exc:  # noqa: BLE001 - never crash a scheduled job
        logger.exception("Failed to build weekly report")
        try:
            from notifier import send_error_alert
            send_error_alert(f"weekly_report build failed: {exc}")
        except Exception:  # noqa: BLE001
            logger.exception("Failed to deliver error alert")
        return 1

    try:
        import requests

        from config import TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID

        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        payload = {
            "chat_id": TELEGRAM_CHAT_ID,
            "text": report,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }
        response = requests.post(url, json=payload, timeout=10)
        if response.status_code == 200 and response.json().get("ok"):
            logger.info("Weekly report delivered (%d records)", len(records))
            return 0
        logger.error("Weekly report delivery failed: %s %s",
                     response.status_code, response.text[:300])
        return 1
    except Exception as exc:  # noqa: BLE001
        logger.exception("Weekly report delivery error: %s", exc)
        return 1


if __name__ == "__main__":
    sys.exit(main())
