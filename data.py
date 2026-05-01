"""Twelve Data market-data client for xaubot.

Wraps the Twelve Data REST API to fetch OHLCV candles for the
configured symbol on multiple timeframes. Public method signatures
are identical to the previous yfinance client so nothing else needs
to change. All network errors and bad payloads are caught and logged;
public methods return None on failure.

Free-tier budget: 800 credits/day, 8 credits/minute.
Per cycle: 5 requests (M15/H1/D1 XAU + H1/D1 DXY).
96 cycles/day × 5 = 480 credits — comfortably within 800/day limit.
"""

from __future__ import annotations

import logging
import time
from typing import Optional

import pandas as pd
import requests

from config import TWELVE_DATA_API_KEY

logger = logging.getLogger(__name__)

_BASE_URL = "https://api.twelvedata.com/time_series"
_RETRY_ATTEMPTS = 3
_RETRY_BACKOFF_SECONDS = (2, 5, 10)

_SYMBOL_MAP = {
    "XAU/USD": "XAU/USD",
    "DXY": "DXY",
}

_INTERVAL_MAP = {
    "15min": "15min",
    "1h": "1h",
    "1day": "1day",
}


class MarketDataClient:
    """Market-data client backed by the Twelve Data REST API."""

    def __init__(self, api_key: Optional[str] = None) -> None:
        self._api_key = api_key or TWELVE_DATA_API_KEY

    def _fetch(
        self, symbol: str, interval: str, outputsize: int
    ) -> Optional[pd.DataFrame]:
        """Hit the Twelve Data time-series endpoint with retry/backoff.

        Returns a DataFrame sorted oldest→newest with columns
        [time, open, high, low, close, volume], or None on failure.
        """
        td_interval = _INTERVAL_MAP.get(interval)
        if td_interval is None:
            logger.error("Unsupported interval %r", interval)
            return None

        params = {
            "symbol": symbol,
            "interval": td_interval,
            "outputsize": outputsize,
            "apikey": self._api_key,
        }

        last_exc: Optional[Exception] = None
        for attempt in range(_RETRY_ATTEMPTS):
            try:
                resp = requests.get(_BASE_URL, params=params, timeout=15)
                resp.raise_for_status()
                payload = resp.json()
            except Exception as exc:  # noqa: BLE001 - network/parse error
                last_exc = exc
                logger.warning(
                    "Twelve Data attempt %d/%d failed for %s %s: %s",
                    attempt + 1, _RETRY_ATTEMPTS, symbol, interval, exc,
                )
                if attempt < _RETRY_ATTEMPTS - 1:
                    time.sleep(_RETRY_BACKOFF_SECONDS[attempt])
                continue

            status = payload.get("status")
            if status == "error":
                code = payload.get("code")
                msg = payload.get("message", "unknown error")
                logger.warning(
                    "Twelve Data API error for %s %s (code=%s): %s",
                    symbol, interval, code, msg,
                )
                # 429 = rate limited — back off and retry
                if code == 429:
                    if attempt < _RETRY_ATTEMPTS - 1:
                        time.sleep(_RETRY_BACKOFF_SECONDS[attempt])
                    continue
                # Other error codes (auth, bad symbol) won't self-heal
                return None

            values = payload.get("values")
            if not values:
                logger.warning(
                    "Twelve Data returned empty values for %s %s",
                    symbol, interval,
                )
                if attempt < _RETRY_ATTEMPTS - 1:
                    time.sleep(_RETRY_BACKOFF_SECONDS[attempt])
                continue

            try:
                df = pd.DataFrame(values)
                df["time"] = pd.to_datetime(df["datetime"], utc=True)
                for col in ("open", "high", "low", "close"):
                    df[col] = pd.to_numeric(df[col], errors="coerce")
                if "volume" in df.columns:
                    df["volume"] = pd.to_numeric(df["volume"], errors="coerce").fillna(0)
                else:
                    df["volume"] = 0
                df = df[["time", "open", "high", "low", "close", "volume"]]
                # Twelve Data returns newest-first; reverse for TA libs
                df = df.sort_values("time").reset_index(drop=True)
                return df
            except Exception as exc:  # noqa: BLE001 - defensive parse guard
                logger.error(
                    "Failed parsing Twelve Data payload for %s %s: %s",
                    symbol, interval, exc,
                )
                return None

        logger.error(
            "Twelve Data exhausted retries for %s %s: %s",
            symbol, interval, last_exc,
        )
        return None

    def get_candles(
        self,
        symbol: str,
        interval: str,
        outputsize: int = 100,
    ) -> Optional[pd.DataFrame]:
        """Fetch OHLCV candles for a single symbol/interval.

        Args:
            symbol: Symbol like "XAU/USD" or "DXY".
            interval: One of "15min", "1h", "1day".
            outputsize: Number of recent candles to return (max 5000).

        Returns:
            DataFrame sorted oldest→newest, or None on failure.
        """
        td_symbol = _SYMBOL_MAP.get(symbol, symbol)
        return self._fetch(td_symbol, interval, outputsize)

    def get_multi_timeframe(self, symbol: str) -> Optional[dict]:
        """Fetch M15, H1, and D1 candles for the primary symbol.

        Args:
            symbol: Symbol such as "XAU/USD".

        Returns:
            Dict with keys "M15", "H1", "D1" mapping to DataFrames,
            or None if any fetch fails.
        """
        m15 = self.get_candles(symbol, "15min", outputsize=200)
        h1 = self.get_candles(symbol, "1h", outputsize=250)
        d1 = self.get_candles(symbol, "1day", outputsize=260)

        if m15 is None or h1 is None or d1 is None:
            logger.error(
                "Multi-timeframe fetch incomplete for %s (m15=%s h1=%s d1=%s)",
                symbol, m15 is not None, h1 is not None, d1 is not None,
            )
            return None
        return {"M15": m15, "H1": h1, "D1": d1}

    def get_dxy_context(self) -> Optional[dict]:
        """Fetch DXY H1 + D1 candles for cross-asset context.

        Returns:
            Dict with keys "symbol", "H1", "D1", or None if DXY
            is unavailable. Caller should continue without it.
        """
        h1 = self.get_candles("DXY", "1h", outputsize=250)
        d1 = self.get_candles("DXY", "1day", outputsize=260)
        if h1 is None or d1 is None:
            logger.warning("DXY context unavailable; continuing without it")
            return None
        return {"symbol": "DXY", "H1": h1, "D1": d1}
