"""Telegram notifier for trade signals and error alerts.

Uses the raw Telegram Bot HTTP API via ``requests`` (no third-party
Telegram library) to deliver formatted signal messages and crash
alerts. All network failures are caught and logged; functions return
False/None rather than raising so the scheduler loop stays alive.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

import requests

from config import TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID

logger = logging.getLogger(__name__)

_TELEGRAM_API = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
_REQUEST_TIMEOUT = 10

_SIGNAL_EMOJI = {"BUY": "⬆️", "SELL": "⬇️", "WAIT": "⏸️"}


def _format_signal_message(signal: dict, price: float) -> str:
    """Render a Telegram-friendly message body for a signal dict.

    Args:
        signal: Signal dict from analyst.analyze.
        price: Current market price at send time.

    Returns:
        Formatted message string.
    """
    direction = signal.get("signal", "WAIT")
    emoji = _SIGNAL_EMOJI.get(direction, "")
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    return (
        f"{emoji} <b>XAUUSD {direction}</b>\n"
        f"<b>Confidence:</b> {signal.get('confidence', '?')}%\n"
        f"<b>Entry:</b> {signal.get('entry_zone', 'n/a')}\n"
        f"<b>Stop loss:</b> {signal.get('stop_loss', 'n/a')}\n"
        f"<b>Take profit:</b> {signal.get('take_profit', 'n/a')}\n"
        f"<b>Bias:</b> {signal.get('timeframe_bias', 'n/a')}\n"
        f"<b>Reasoning:</b> {signal.get('reasoning', 'n/a')}\n"
        f"\n<b>Price:</b> {price}\n"
        f"<b>Time:</b> {timestamp}"
    )


def _post(payload: dict) -> bool:
    """POST a payload to the Telegram sendMessage endpoint.

    Args:
        payload: JSON payload for sendMessage.

    Returns:
        True on HTTP 200 + Telegram ``ok=true``, False otherwise.
    """
    try:
        response = requests.post(_TELEGRAM_API, json=payload, timeout=_REQUEST_TIMEOUT)
    except requests.RequestException as exc:
        logger.error("Telegram request failed: %s", exc)
        return False

    if response.status_code != 200:
        logger.error("Telegram bad status %s: %s", response.status_code, response.text[:300])
        return False

    try:
        body = response.json()
    except ValueError:
        logger.error("Telegram returned non-JSON: %s", response.text[:300])
        return False

    if not body.get("ok"):
        logger.error("Telegram API rejected message: %s", body)
        return False
    return True


def send_signal(signal: dict, price: float) -> bool:
    """Send a formatted trade signal to the configured Telegram chat.

    Args:
        signal: Signal dict from analyst.analyze.
        price: Current market price.

    Returns:
        True on success, False on any failure.
    """
    text = _format_signal_message(signal, price)
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    ok = _post(payload)
    if ok:
        logger.info("Telegram signal delivered: %s @ %s", signal.get("signal"), price)
    else:
        logger.error("Telegram signal delivery failed: %s @ %s", signal.get("signal"), price)
    return ok


def send_error_alert(message: str) -> None:
    """Send a plaintext error/crash alert to Telegram.

    Args:
        message: Short description of the error or event.
    """
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": f"⚠️ xaubot alert\n{message}\n\n{timestamp}",
        "disable_web_page_preview": True,
    }
    if _post(payload):
        logger.info("Telegram error alert sent: %s", message)
    else:
        logger.error("Telegram error alert failed: %s", message)
