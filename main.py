"""Main orchestration loop for xaubot.

Configures logging to both console and ``xaubot.log``, validates
config at import time, sends a Telegram startup notification, then
schedules a 15-minute cycle that fetches multi-timeframe candles,
computes indicators, runs the Claude analyst, applies filters, and
delivers approved signals to Telegram. Designed to run forever as
a systemd service; KeyboardInterrupt triggers a clean shutdown.
"""

from __future__ import annotations

import logging
import sys
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from typing import Optional

from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger


def _configure_logging() -> None:
    """Wire up the root logger to console and rotating file output."""
    root = logging.getLogger()
    if root.handlers:
        return
    root.setLevel(logging.INFO)

    fmt = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(fmt)
    root.addHandler(console)

    file_handler = RotatingFileHandler(
        "xaubot.log", maxBytes=2_000_000, backupCount=3, encoding="utf-8"
    )
    file_handler.setFormatter(fmt)
    root.addHandler(file_handler)


_configure_logging()
logger = logging.getLogger("xaubot.main")

import config  # noqa: E402  - import after logging config so its logs are captured
from analyst import analyze  # noqa: E402
from data import MarketDataClient  # noqa: E402
from filters import evaluate_filters  # noqa: E402
from indicators import compute_indicators  # noqa: E402
from notifier import send_error_alert, send_signal  # noqa: E402
from signal_log import log_signal  # noqa: E402
from telegram_bot import start_command_listener  # noqa: E402


_client = MarketDataClient()

_last_signal: Optional[str] = None
_last_signal_time: Optional[datetime] = None
_last_confidence: Optional[int] = None


def run_cycle() -> None:
    """Execute one fetch -> analyze -> filter -> notify cycle.

    All exceptions are captured and reported to Telegram so that a
    transient failure never tears down the scheduler.
    """
    global _last_signal, _last_signal_time, _last_confidence

    cycle_start = datetime.now(timezone.utc).isoformat()
    logger.info("=== Cycle start %s ===", cycle_start)

    try:
        candles = _client.get_multi_timeframe(config.SYMBOL)
        if candles is None:
            logger.warning("Skipping cycle: candle fetch failed")
            return

        indicators_m15 = compute_indicators(candles["M15"])
        indicators_h1 = compute_indicators(candles["H1"])
        indicators_d1 = compute_indicators(candles["D1"])

        if not indicators_m15 or not indicators_h1 or not indicators_d1:
            logger.warning("Skipping cycle: indicator computation produced empty result")
            return

        current_price = indicators_m15.get("close")
        if current_price is None:
            logger.warning("Skipping cycle: current price unavailable")
            return

        # Cross-timeframe sanity check — yfinance occasionally returns
        # stale or mismatched bars across timeframes for the same
        # instrument. If the closes diverge by more than 0.5% the
        # analyst would be reasoning over inconsistent worlds.
        h1_close = indicators_h1.get("close")
        d1_close = indicators_d1.get("close")
        if h1_close is not None and d1_close is not None:
            max_div = max(abs(h1_close - current_price), abs(d1_close - current_price))
            if max_div > current_price * 0.005:
                logger.error(
                    "Skipping cycle: cross-timeframe mismatch "
                    "(M15=%.2f H1=%.2f D1=%.2f, max_div=%.2f)",
                    current_price, h1_close, d1_close, max_div,
                )
                return

        # Chop pre-filter — when both H1 and D1 are below the ADX
        # threshold the market is ranging and trend setups have negative
        # expectancy. Skip the analyst call entirely to save tokens.
        if config.CHOP_ADX_THRESHOLD > 0:
            h1_adx = indicators_h1.get("adx")
            d1_adx = indicators_d1.get("adx")
            if (
                h1_adx is not None
                and d1_adx is not None
                and h1_adx < config.CHOP_ADX_THRESHOLD
                and d1_adx < config.CHOP_ADX_THRESHOLD
            ):
                logger.info(
                    "Skipping cycle: chop regime (H1 ADX=%.1f, D1 ADX=%.1f, threshold=%.1f)",
                    h1_adx, d1_adx, config.CHOP_ADX_THRESHOLD,
                )
                return

        dxy_raw = _client.get_dxy_context()
        if dxy_raw is not None:
            dxy_context = {
                "symbol": dxy_raw["symbol"],
                "H1": compute_indicators(dxy_raw["H1"]),
                "D1": compute_indicators(dxy_raw["D1"]),
            }
        else:
            dxy_context = None

        signal = analyze(indicators_m15, indicators_h1, indicators_d1, dxy_context)
        if signal is None:
            logger.warning("Skipping cycle: analyst returned no signal")
            return

        logger.info(
            "Analyst output: %s @ confidence=%s",
            signal.get("signal"),
            signal.get("confidence"),
        )

        if signal["signal"] == "WAIT":
            filter_passed, filter_reason = False, "wait_signal_not_delivered"
        else:
            filter_passed, filter_reason = evaluate_filters(
                signal,
                _last_signal,
                _last_signal_time,
                last_confidence=_last_confidence,
                min_confidence=config.CONFIDENCE_MIN,
                cooldown_minutes=config.COOLDOWN_MINUTES,
                confidence_bump=config.DUP_CONFIDENCE_BUMP,
                session_enabled=config.SESSION_FILTER_ENABLED,
                min_rr=config.MIN_RR,
            )

        delivered = False
        if filter_passed:
            delivered = send_signal(signal, current_price)
            if delivered:
                _last_signal = signal["signal"]
                _last_signal_time = datetime.now(timezone.utc)
                try:
                    _last_confidence = int(signal.get("confidence", 0))
                except (TypeError, ValueError):
                    _last_confidence = None

        log_signal(
            signal,
            current_price,
            indicators_m15,
            indicators_h1,
            filter_passed,
            filter_reason,
            delivered,
            indicators_d1=indicators_d1,
            dxy_context=dxy_context,
        )

        if not filter_passed:
            logger.info("Signal logged to signals.log; not delivered (%s)", filter_reason)
        elif delivered:
            logger.info("Cycle complete: signal delivered and logged")
        else:
            logger.error("Cycle complete: signal delivery failed (logged)")

    except Exception as exc:  # noqa: BLE001 - top-level safety net
        logger.exception("Unhandled error in run_cycle: %s", exc)
        try:
            send_error_alert(f"run_cycle error: {exc}")
        except Exception:  # noqa: BLE001
            logger.exception("Failed to deliver error alert")


def main() -> None:
    """Bot entry point: announce startup, run once, then schedule."""
    logger.info("xaubot starting up — symbol=%s confidence_min=%s",
                config.SYMBOL, config.CONFIDENCE_MIN)
    try:
        send_error_alert(
            f"xaubot started ({config.SYMBOL}, "
            f"conf>={config.CONFIDENCE_MIN}%)."
        )
    except Exception:  # noqa: BLE001
        logger.exception("Startup Telegram notification failed")

    start_command_listener()

    logger.info("Running first cycle immediately on startup")
    run_cycle()

    scheduler = BlockingScheduler(timezone="UTC")
    scheduler.add_job(
        run_cycle,
        trigger=CronTrigger(minute="0,15,30,45"),
        id="xaubot-cycle",
        max_instances=1,
        coalesce=True,
    )

    logger.info("Scheduler armed: cron minute=0,15,30,45 (UTC)")
    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        logger.info("Shutdown signal received, stopping scheduler")
        scheduler.shutdown(wait=False)


if __name__ == "__main__":
    main()
