"""Configuration loader for xaubot.

Loads environment variables from a local .env file via python-dotenv,
exposes them as typed module-level constants, and validates that all
required keys are present at import time. If a required key is missing
the process logs a clear error and exits early with status 1.
"""

from __future__ import annotations

import logging
import os
import sys

from dotenv import load_dotenv

logger = logging.getLogger(__name__)

load_dotenv()


def _require(name: str) -> str:
    """Return the value of a required env var or exit the process.

    Args:
        name: The environment variable name.

    Returns:
        The string value of the environment variable.
    """
    value = os.getenv(name)
    if not value or not value.strip():
        sys.stderr.write(
            f"[config] FATAL: required environment variable '{name}' is missing. "
            f"Copy .env.example to .env and fill in all keys.\n"
        )
        sys.exit(1)
    return value.strip()


def _optional(name: str, default: str) -> str:
    """Return an optional env var value or a default.

    Args:
        name: The environment variable name.
        default: Default value to use if the variable is unset/empty.

    Returns:
        The environment variable value or the supplied default.
    """
    value = os.getenv(name)
    if value is None or not value.strip():
        return default
    return value.strip()


def _bool(name: str, default: bool) -> bool:
    """Parse a boolean env var ('1','true','yes','on' = True)."""
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


ANTHROPIC_API_KEY: str = _require("ANTHROPIC_API_KEY")
TWELVE_DATA_API_KEY: str = _require("TWELVE_DATA_API_KEY")
TELEGRAM_BOT_TOKEN: str = _require("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID: str = _require("TELEGRAM_CHAT_ID")

SYMBOL: str = _optional("SYMBOL", "XAU/USD")
CONFIDENCE_MIN: int = int(_optional("CONFIDENCE_MIN", "65"))
TIMEFRAME_PRIMARY: str = _optional("TIMEFRAME_PRIMARY", "15min")
TIMEFRAME_HIGHER: str = _optional("TIMEFRAME_HIGHER", "1h")

# Duplicate-filter tuning. Cooldown is the minimum gap between same-direction
# signals; bump lets a stronger conviction bypass cooldown if confidence rose
# by at least N points vs. the last delivered signal in the same direction.
COOLDOWN_MINUTES: int = int(_optional("COOLDOWN_MINUTES", "30"))
DUP_CONFIDENCE_BUMP: int = int(_optional("DUP_CONFIDENCE_BUMP", "10"))

# Session filter. Set SESSION_FILTER_ENABLED=false to allow signals 24/5
# (Asian session included).
SESSION_FILTER_ENABLED: bool = _bool("SESSION_FILTER_ENABLED", True)

# Minimum reward:risk ratio. Setups below this are rejected — at 40%
# hit rate, 2.0 is the breakeven threshold; at 50% hit rate, 1.5 is.
MIN_RR: float = float(_optional("MIN_RR", "2.0"))

# Regime pre-filter. When BOTH D1 and H1 ADX fall below this value the
# market is treated as choppy and the cycle is skipped before calling
# Claude — most TA edges disappear in chop. Set to 0 to disable.
CHOP_ADX_THRESHOLD: float = float(_optional("CHOP_ADX_THRESHOLD", "20"))
