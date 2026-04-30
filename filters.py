"""Pre-trade signal filters.

A small library of boolean gates the main loop applies before
delivering a signal to Telegram: trading-session window, minimum
confidence, and a per-direction cooldown that suppresses duplicate
signals. ``all_filters_pass`` composes them and logs which gate
blocked a signal so cycle decisions are auditable from the log file.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Optional, Tuple

logger = logging.getLogger(__name__)


def session_filter() -> bool:
    """Return True only during the London or New York trading session.

    Sessions are evaluated in UTC:
    - London: 07:00-16:00
    - New York: 12:00-21:00

    Returns:
        True if the current UTC hour falls inside either window.
    """
    now_utc = datetime.now(timezone.utc)
    hour = now_utc.hour
    in_london = 7 <= hour < 16
    in_ny = 12 <= hour < 21
    return in_london or in_ny


def confidence_filter(confidence: int, min_confidence: int) -> bool:
    """Return True if signal confidence meets the threshold.

    Args:
        confidence: Reported signal confidence (0-100).
        min_confidence: Configured minimum threshold.

    Returns:
        True if ``confidence >= min_confidence``.
    """
    try:
        return int(confidence) >= int(min_confidence)
    except (TypeError, ValueError):
        return False


def duplicate_filter(
    new_signal: str,
    last_signal: Optional[str],
    last_signal_time: Optional[datetime],
    cooldown_minutes: int = 60,
) -> bool:
    """Return False if the same direction recently fired.

    Args:
        new_signal: The freshly-produced direction (BUY/SELL/WAIT).
        last_signal: The previously-sent direction, if any.
        last_signal_time: Timestamp of the previously-sent signal.
        cooldown_minutes: Window in minutes during which a repeat of
            the same direction is suppressed.

    Returns:
        True if the new signal is allowed; False if it duplicates a
        recent signal in the same direction.
    """
    if last_signal is None or last_signal_time is None:
        return True
    if new_signal != last_signal:
        return True
    elapsed = datetime.now(timezone.utc) - last_signal_time
    return elapsed >= timedelta(minutes=cooldown_minutes)


def evaluate_filters(
    signal_dict: dict,
    last_signal: Optional[str],
    last_signal_time: Optional[datetime],
    min_confidence: int = 65,
    cooldown_minutes: int = 60,
) -> Tuple[bool, str]:
    """Run every filter and return (passed, reason).

    Args:
        signal_dict: Parsed signal dict from analyst.analyze.
        last_signal: Previous direction sent.
        last_signal_time: Timestamp of the previous send (UTC).
        min_confidence: Confidence threshold.
        cooldown_minutes: Duplicate-suppression window.

    Returns:
        Tuple of ``(passed, reason)``. ``reason`` is ``"ok"`` when all
        filters pass, or a short identifier of the failing filter
        otherwise. The caller is responsible for logging the reason.
    """
    if not session_filter():
        return False, "session_filter (outside London/NY hours)"

    direction = str(signal_dict.get("signal", "")).upper()
    confidence = signal_dict.get("confidence", 0)

    if not confidence_filter(confidence, min_confidence):
        return False, f"confidence_filter ({confidence} < {min_confidence})"

    if not duplicate_filter(direction, last_signal, last_signal_time, cooldown_minutes):
        return False, f"duplicate_filter ({direction} within {cooldown_minutes}min cooldown)"

    return True, "ok"


def all_filters_pass(
    signal_dict: dict,
    last_signal: Optional[str],
    last_signal_time: Optional[datetime],
    min_confidence: int = 65,
    cooldown_minutes: int = 60,
) -> bool:
    """Boolean wrapper around ``evaluate_filters`` that logs blocks.

    Args:
        signal_dict: Parsed signal dict from analyst.analyze.
        last_signal: Previous direction sent.
        last_signal_time: Timestamp of the previous send (UTC).
        min_confidence: Confidence threshold.
        cooldown_minutes: Duplicate-suppression window.

    Returns:
        True only if every filter is satisfied.
    """
    passed, reason = evaluate_filters(
        signal_dict, last_signal, last_signal_time, min_confidence, cooldown_minutes
    )
    if not passed:
        logger.info("Filter blocked signal: %s", reason)
    return passed
