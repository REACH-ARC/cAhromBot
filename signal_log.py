"""Append-only JSONL log of every analyst signal.

Records one JSON object per analyst output regardless of whether the
signal was eventually delivered to Telegram. Useful for shadow-mode
review: collect a few weeks of data, then bucket by confidence to
decide whether the live ``CONFIDENCE_MIN`` threshold is set correctly.
Each record includes the full M15/H1 indicator snapshots so a record
is self-contained and can be replayed offline.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger(__name__)

_LOG_PATH = "signals.log"


def log_signal(
    signal: dict,
    price: Optional[float],
    indicators_m15: dict,
    indicators_h1: dict,
    filter_passed: bool,
    filter_reason: str,
    delivered: bool,
    indicators_d1: Optional[dict] = None,
    dxy_context: Optional[dict] = None,
) -> None:
    """Append one record describing an analyst signal to ``signals.log``.

    Args:
        signal: Parsed signal dict from analyst.analyze.
        price: Current market price at evaluation time.
        indicators_m15: M15 indicator snapshot.
        indicators_h1: H1 indicator snapshot.
        filter_passed: Whether the filter gate would have allowed delivery.
        filter_reason: Short reason string from evaluate_filters.
        delivered: Whether a Telegram message was actually sent.
        indicators_d1: D1 indicator snapshot. Optional.
        dxy_context: Dict with H1/D1 indicator snapshots for DXY plus
            the resolved Twelve Data symbol. Optional.
    """
    record = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "signal": signal.get("signal"),
        "confidence": signal.get("confidence"),
        "entry_zone": signal.get("entry_zone"),
        "stop_loss": signal.get("stop_loss"),
        "take_profit": signal.get("take_profit"),
        "reasoning": signal.get("reasoning"),
        "timeframe_bias": signal.get("timeframe_bias"),
        "price": price,
        "filter_passed": filter_passed,
        "filter_reason": filter_reason,
        "delivered": delivered,
        "m15": indicators_m15,
        "h1": indicators_h1,
        "d1": indicators_d1,
        "dxy": dxy_context,
    }
    try:
        with open(_LOG_PATH, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(record) + "\n")
    except Exception as exc:  # noqa: BLE001 - logging must never crash the loop
        logger.error("Failed to append to signals.log: %s", exc)
