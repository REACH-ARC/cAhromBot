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


ANTHROPIC_API_KEY: str = _require("ANTHROPIC_API_KEY")
TELEGRAM_BOT_TOKEN: str = _require("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID: str = _require("TELEGRAM_CHAT_ID")

SYMBOL: str = _optional("SYMBOL", "XAU/USD")
CONFIDENCE_MIN: int = int(_optional("CONFIDENCE_MIN", "65"))
TIMEFRAME_PRIMARY: str = _optional("TIMEFRAME_PRIMARY", "15min")
TIMEFRAME_HIGHER: str = _optional("TIMEFRAME_HIGHER", "1h")
