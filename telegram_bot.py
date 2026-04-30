"""Telegram command listener for xaubot.

Runs a long-polling background thread that watches for incoming
commands in the configured chat. The only command currently handled
is ``/log``, which uploads the most recent ``signals.log`` file as
a Telegram document. All other messages are ignored, and messages
from any chat other than ``TELEGRAM_CHAT_ID`` are dropped silently
so a bot accidentally added elsewhere can't be queried.

Implementation notes:
- Long polling via ``getUpdates`` with ``timeout=30`` keeps the
  connection open until an update arrives, minimizing API calls.
- On startup we discard any pending unprocessed updates so that
  commands sent while the bot was offline do NOT replay.
- The polling thread is a daemon so it dies with the main process.
- All exceptions inside the loop are caught — the listener must
  never crash the bot.
"""

from __future__ import annotations

import logging
import threading
import time
from pathlib import Path
from typing import Optional

import requests

from config import TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID

logger = logging.getLogger(__name__)

_API_BASE = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"
_LONG_POLL_TIMEOUT = 30
_REQUEST_TIMEOUT = _LONG_POLL_TIMEOUT + 5
_SIGNALS_LOG_PATH = Path(__file__).parent / "signals.log"
_MAX_DOCUMENT_BYTES = 50 * 1024 * 1024  # Telegram's 50 MB document limit


def _send_document(path: Path, caption: str = "") -> bool:
    """Upload a file to the configured chat as a Telegram document.

    Args:
        path: Filesystem path to the file to upload.
        caption: Optional caption text.

    Returns:
        True on success, False on any failure.
    """
    try:
        with path.open("rb") as fh:
            response = requests.post(
                f"{_API_BASE}/sendDocument",
                data={"chat_id": TELEGRAM_CHAT_ID, "caption": caption},
                files={"document": (path.name, fh)},
                timeout=60,
            )
    except requests.RequestException as exc:
        logger.error("Telegram sendDocument network error: %s", exc)
        return False
    if response.status_code != 200:
        logger.error("Telegram sendDocument bad status %s: %s",
                     response.status_code, response.text[:300])
        return False
    body = response.json() if response.text else {}
    if not body.get("ok"):
        logger.error("Telegram sendDocument rejected: %s", body)
        return False
    return True


def _send_message(text: str) -> None:
    """Send a plain-text reply (used to report command errors).

    Args:
        text: Message body.
    """
    try:
        requests.post(
            f"{_API_BASE}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": text},
            timeout=10,
        )
    except requests.RequestException as exc:
        logger.error("Telegram sendMessage failed: %s", exc)


def _handle_log() -> None:
    """Respond to ``/log`` by uploading ``signals.log``."""
    if not _SIGNALS_LOG_PATH.exists():
        _send_message("No signals.log yet — the bot has not run any cycles.")
        return
    size = _SIGNALS_LOG_PATH.stat().st_size
    if size == 0:
        _send_message("signals.log is empty.")
        return
    if size > _MAX_DOCUMENT_BYTES:
        _send_message(
            f"signals.log is {size // 1_000_000} MB — exceeds Telegram's "
            f"50 MB document limit."
        )
        return
    if _send_document(_SIGNALS_LOG_PATH, caption=f"signals.log ({size:,} bytes)"):
        logger.info("Delivered signals.log via /log command (%d bytes)", size)


def _process_update(update: dict) -> None:
    """Inspect one update from getUpdates and dispatch known commands.

    Args:
        update: Raw update object from the Telegram getUpdates response.
    """
    message = update.get("message") or update.get("channel_post")
    if not message:
        return

    chat_id = message.get("chat", {}).get("id")
    if str(chat_id) != str(TELEGRAM_CHAT_ID):
        logger.debug("Ignoring message from foreign chat_id=%s", chat_id)
        return

    text = (message.get("text") or "").strip()
    if not text.startswith("/"):
        return

    command = text.split()[0].lower().split("@", 1)[0]
    if command == "/log":
        _handle_log()


def _drain_pending_updates() -> Optional[int]:
    """Mark all currently-pending updates as read so we don't replay them.

    Returns:
        The next ``update_id`` to use as offset, or ``None`` if there
        were no pending updates.
    """
    try:
        response = requests.get(
            f"{_API_BASE}/getUpdates",
            params={"offset": -1, "timeout": 0},
            timeout=10,
        )
    except requests.RequestException as exc:
        logger.warning("Could not drain pending Telegram updates: %s", exc)
        return None
    if response.status_code != 200:
        return None
    results = response.json().get("result", []) or []
    if not results:
        return None
    return results[-1]["update_id"] + 1


def _polling_loop() -> None:
    """Long-poll Telegram forever, dispatching recognized commands."""
    offset = _drain_pending_updates()
    logger.info("Telegram command listener started (long polling, offset=%s)", offset)

    while True:
        try:
            params = {
                "timeout": _LONG_POLL_TIMEOUT,
                "allowed_updates": '["message","channel_post"]',
            }
            if offset is not None:
                params["offset"] = offset

            response = requests.get(
                f"{_API_BASE}/getUpdates",
                params=params,
                timeout=_REQUEST_TIMEOUT,
            )
            if response.status_code != 200:
                logger.error("Telegram getUpdates bad status: %s", response.status_code)
                time.sleep(5)
                continue

            body = response.json()
            if not body.get("ok"):
                logger.error("Telegram getUpdates not ok: %s", body)
                time.sleep(5)
                continue

            for update in body.get("result", []):
                offset = update["update_id"] + 1
                try:
                    _process_update(update)
                except Exception:  # noqa: BLE001 - listener must not crash
                    logger.exception("Failed processing Telegram update")
        except requests.RequestException as exc:
            logger.error("Telegram polling network error: %s", exc)
            time.sleep(5)
        except Exception:  # noqa: BLE001 - listener must not crash
            logger.exception("Unexpected error in polling loop")
            time.sleep(5)


def start_command_listener() -> threading.Thread:
    """Spawn the long-polling listener as a daemon thread.

    Returns:
        The started thread (daemon=True, dies with the main process).
    """
    thread = threading.Thread(
        target=_polling_loop,
        daemon=True,
        name="xaubot-tg-listener",
    )
    thread.start()
    return thread
