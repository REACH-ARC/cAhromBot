"""Pre-trade signal filters.

A small library of boolean gates the main loop applies before
delivering a signal to Telegram: trading-session window, minimum
confidence, and a per-direction cooldown that suppresses duplicate
signals (with a confidence-bump bypass for stronger convictions).
``evaluate_filters`` composes them and returns a reason string so cycle
decisions are auditable from the log file.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Optional, Tuple

logger = logging.getLogger(__name__)

_PRICE_RE = re.compile(r"\d+(?:\.\d+)?")


def _parse_price(value: object) -> Optional[float]:
    """Extract a single float from a Claude-formatted price string.

    Accepts plain numbers (``"4602.00"``), ranges (``"4585.00-4592.00"``
    → midpoint), and the unicode dashes Claude sometimes emits.

    Args:
        value: Raw value from the signal dict.

    Returns:
        Parsed float, or None if no number can be extracted.
    """
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    nums = _PRICE_RE.findall(str(value).replace(",", ""))
    if not nums:
        return None
    if len(nums) == 1:
        return float(nums[0])
    return (float(nums[0]) + float(nums[1])) / 2.0


def compute_rr(signal_dict: dict) -> Optional[float]:
    """Compute the reward:risk ratio implied by a signal's levels.

    Uses the entry-zone midpoint as the entry; risk is the distance to
    stop_loss, reward is the distance to take_profit. Direction-aware.

    Args:
        signal_dict: Parsed signal dict from analyst.analyze.

    Returns:
        R:R as a float, or None if any level cannot be parsed or the
        levels are inconsistent (zero/negative risk or reward).
    """
    direction = str(signal_dict.get("signal", "")).upper()
    if direction not in ("BUY", "SELL"):
        return None

    entry = _parse_price(signal_dict.get("entry_zone"))
    sl = _parse_price(signal_dict.get("stop_loss"))
    tp = _parse_price(signal_dict.get("take_profit"))
    if entry is None or sl is None or tp is None:
        return None

    if direction == "BUY":
        risk = entry - sl
        reward = tp - entry
    else:  # SELL
        risk = sl - entry
        reward = entry - tp

    if risk <= 0 or reward <= 0:
        return None
    return reward / risk


def rr_filter(signal_dict: dict, min_rr: float) -> Tuple[bool, Optional[float]]:
    """Return (passed, computed_rr).

    Args:
        signal_dict: Parsed signal dict from analyst.analyze.
        min_rr: Minimum acceptable reward:risk ratio.

    Returns:
        Tuple of (passed, rr). ``rr`` is the computed ratio (may be
        None if levels were unparseable, in which case ``passed`` is
        also False).
    """
    rr = compute_rr(signal_dict)
    if rr is None:
        return False, None
    return rr >= float(min_rr), rr


def session_filter(enabled: bool = True) -> bool:
    """Return True if the current UTC time is inside an allowed session.

    Sessions are evaluated in UTC:
    - London: 07:00-16:00
    - New York: 12:00-21:00

    Args:
        enabled: When False the filter is a no-op (always passes).
            Useful for trading the Asian session or for backtests.

    Returns:
        True if the filter passes (i.e. signal is allowed).
    """
    if not enabled:
        return True
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
    new_confidence: int,
    last_signal: Optional[str],
    last_signal_time: Optional[datetime],
    last_confidence: Optional[int],
    cooldown_minutes: int = 30,
    confidence_bump: int = 10,
) -> bool:
    """Return False if the same direction recently fired without escalation.

    The cooldown is bypassed when the new signal's confidence has risen
    by ``confidence_bump`` points or more compared to the previous one
    in the same direction — the rationale being that a strengthening
    setup is materially different information, not a duplicate.

    Args:
        new_signal: The freshly-produced direction (BUY/SELL/WAIT).
        new_confidence: The freshly-produced confidence (0-100).
        last_signal: The previously-delivered direction, if any.
        last_signal_time: Timestamp of the previously-delivered signal.
        last_confidence: Confidence of the previously-delivered signal.
        cooldown_minutes: Window in minutes during which a same-direction
            repeat without a confidence bump is suppressed.
        confidence_bump: Minimum confidence increase that bypasses the
            cooldown.

    Returns:
        True if the new signal is allowed; False if it duplicates a
        recent signal in the same direction without a confidence bump.
    """
    if last_signal is None or last_signal_time is None:
        return True
    if new_signal != last_signal:
        return True
    elapsed = datetime.now(timezone.utc) - last_signal_time
    if elapsed >= timedelta(minutes=cooldown_minutes):
        return True
    # Inside cooldown — allow only if the new conviction is materially stronger.
    try:
        new_c = int(new_confidence)
        last_c = int(last_confidence) if last_confidence is not None else 0
    except (TypeError, ValueError):
        return False
    return (new_c - last_c) >= confidence_bump


def evaluate_filters(
    signal_dict: dict,
    last_signal: Optional[str],
    last_signal_time: Optional[datetime],
    last_confidence: Optional[int] = None,
    min_confidence: int = 65,
    cooldown_minutes: int = 30,
    confidence_bump: int = 10,
    session_enabled: bool = True,
    min_rr: float = 2.0,
) -> Tuple[bool, str]:
    """Run every filter and return (passed, reason).

    Args:
        signal_dict: Parsed signal dict from analyst.analyze.
        last_signal: Previous direction delivered.
        last_signal_time: Timestamp of the previous delivery (UTC).
        last_confidence: Confidence of the previous delivered signal.
        min_confidence: Confidence threshold.
        cooldown_minutes: Duplicate-suppression window.
        confidence_bump: Confidence rise that bypasses the cooldown.
        session_enabled: When False, session filter is a no-op.
        min_rr: Minimum reward:risk ratio. Set to 0 to disable.

    Returns:
        Tuple of ``(passed, reason)``. ``reason`` is ``"ok"`` when all
        filters pass, or a short identifier of the failing filter
        otherwise.
    """
    if not session_filter(session_enabled):
        return False, "session_filter (outside London/NY hours)"

    direction = str(signal_dict.get("signal", "")).upper()
    confidence = signal_dict.get("confidence", 0)

    if not confidence_filter(confidence, min_confidence):
        return False, f"confidence_filter ({confidence} < {min_confidence})"

    if min_rr > 0:
        rr_passed, rr = rr_filter(signal_dict, min_rr)
        if not rr_passed:
            if rr is None:
                return False, "rr_filter (could not parse entry/SL/TP)"
            return False, f"rr_filter ({rr:.2f} < {min_rr:.2f})"

    if not duplicate_filter(
        direction,
        confidence,
        last_signal,
        last_signal_time,
        last_confidence,
        cooldown_minutes,
        confidence_bump,
    ):
        return False, (
            f"duplicate_filter ({direction} within {cooldown_minutes}min "
            f"cooldown, conf bump < {confidence_bump})"
        )

    return True, "ok"


def all_filters_pass(
    signal_dict: dict,
    last_signal: Optional[str],
    last_signal_time: Optional[datetime],
    last_confidence: Optional[int] = None,
    min_confidence: int = 65,
    cooldown_minutes: int = 30,
    confidence_bump: int = 10,
    session_enabled: bool = True,
    min_rr: float = 2.0,
) -> bool:
    """Boolean wrapper around ``evaluate_filters`` that logs blocks."""
    passed, reason = evaluate_filters(
        signal_dict,
        last_signal,
        last_signal_time,
        last_confidence,
        min_confidence,
        cooldown_minutes,
        confidence_bump,
        session_enabled,
        min_rr,
    )
    if not passed:
        logger.info("Filter blocked signal: %s", reason)
    return passed
