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


def _format_report(records: List[dict]) -> str:
    """Render the records as a Telegram-friendly summary string.

    Args:
        records: List of signal records from ``_load_recent``.
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
    return "\n".join(lines)


def main() -> int:
    """Build and dispatch the weekly report. Always returns 0/1."""
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")

    try:
        records = _load_recent(_WINDOW_DAYS)
        report = _format_report(records)
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
