"""Yahoo Finance market-data client for xaubot.

Wraps ``yfinance`` to fetch OHLCV candles for the configured symbol
on multiple timeframes. Public method signatures mirror the previous
Twelve Data client so the rest of the bot doesn't need to change.
All network errors and bad payloads are caught and logged; the public
methods return ``None`` on failure so the scheduler loop never crashes.

Symbol mapping for XAU/USD prefers Yahoo's spot pair ``XAUUSD=X`` and
falls back to front-month gold futures ``GC=F`` if that fails. DXY
maps to ``DX-Y.NYB`` (the dollar index) with futures fallback.
"""

from __future__ import annotations

import logging
import time
from typing import Optional

import pandas as pd
import yfinance as yf

logger = logging.getLogger(__name__)

_RETRY_ATTEMPTS = 3
_RETRY_BACKOFF_SECONDS = (2, 5, 10)  # delay before attempt 2, 3, then no-op

_SYMBOL_ALIASES = {
    "XAU/USD": ["GC=F", "XAUUSD=X"],
    "DXY": ["DX-Y.NYB", "DX=F"],
}

_INTERVAL_MAP = {
    "15min": "15m",
    "1h": "1h",
    "1day": "1d",
}

_PERIOD_MAP = {
    "15min": "5d",
    "1h": "60d",
    "1day": "2y",
}


def _flatten(df: pd.DataFrame) -> pd.DataFrame:
    """Flatten yfinance's MultiIndex columns to single-level."""
    if isinstance(df.columns, pd.MultiIndex):
        df = df.copy()
        df.columns = df.columns.get_level_values(0)
    return df


class MarketDataClient:
    """Market-data client backed by Yahoo Finance via yfinance."""

    def __init__(self, api_key: Optional[str] = None) -> None:
        """Initialize the client.

        Args:
            api_key: Ignored. Accepted only for backward compatibility
                with callers that previously instantiated the Twelve
                Data client with an API key.
        """
        del api_key  # yfinance needs no auth

    def _fetch(self, ticker: str, interval: str, period: str) -> Optional[pd.DataFrame]:
        """Yahoo download with bounded retry + exponential backoff.

        Retries on any exception or empty payload, since yfinance
        commonly emits transient 429/5xx errors and silent empties.
        """
        last_exc: Optional[Exception] = None
        for attempt in range(_RETRY_ATTEMPTS):
            try:
                df = yf.download(
                    ticker,
                    period=period,
                    interval=interval,
                    progress=False,
                    auto_adjust=False,
                    threads=False,
                )
            except Exception as exc:  # noqa: BLE001 - any network/parse error
                last_exc = exc
                df = None
                logger.warning(
                    "yfinance attempt %d/%d failed for %s %s: %s",
                    attempt + 1, _RETRY_ATTEMPTS, ticker, interval, exc,
                )
            else:
                if df is not None and not df.empty:
                    return _flatten(df)
                logger.warning(
                    "yfinance attempt %d/%d returned empty for %s %s",
                    attempt + 1, _RETRY_ATTEMPTS, ticker, interval,
                )

            if attempt < _RETRY_ATTEMPTS - 1:
                time.sleep(_RETRY_BACKOFF_SECONDS[attempt])

        if last_exc is not None:
            logger.error(
                "yfinance download exhausted retries for %s %s: %s",
                ticker, interval, last_exc,
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
            symbol: Symbol like "XAU/USD" or "DXY"; mapped to Yahoo
                ticker(s) via ``_SYMBOL_ALIASES``.
            interval: One of ``"15min"``, ``"1h"``, ``"1day"`` (preserved
                from the Twelve Data client interface).
            outputsize: Maximum number of recent candles to return.

        Returns:
            DataFrame with columns ``time, open, high, low, close, volume``
            sorted oldest -> newest, or ``None`` on failure.
        """
        yf_interval = _INTERVAL_MAP.get(interval)
        period = _PERIOD_MAP.get(interval)
        if yf_interval is None or period is None:
            logger.error("Unsupported interval %s", interval)
            return None

        candidates = _SYMBOL_ALIASES.get(symbol, [symbol])
        for ticker in candidates:
            df = self._fetch(ticker, yf_interval, period)
            if df is None or df.empty:
                continue
            try:
                df = df.rename_axis("time").reset_index()
                df["time"] = pd.to_datetime(df["time"], utc=True)
                for col in ("Open", "High", "Low", "Close"):
                    if col in df.columns:
                        df[col] = pd.to_numeric(df[col], errors="coerce")
                if "Volume" not in df.columns:
                    df["Volume"] = 0
                df = df.rename(columns={
                    "Open": "open",
                    "High": "high",
                    "Low": "low",
                    "Close": "close",
                    "Volume": "volume",
                })
                df = df[["time", "open", "high", "low", "close", "volume"]]
                df = df.sort_values("time").reset_index(drop=True)
                df = df.tail(outputsize).reset_index(drop=True)
                if df.empty:
                    continue
                return df
            except Exception as exc:  # noqa: BLE001 - defensive parse guard
                logger.error("Failed parsing yfinance payload for %s: %s", ticker, exc)
                continue

        logger.error("All Yahoo tickers failed for %s %s", symbol, interval)
        return None

    def get_multi_timeframe(self, symbol: str) -> Optional[dict]:
        """Fetch M15, H1, and D1 candles for the primary symbol.

        Args:
            symbol: Symbol such as "XAU/USD".

        Returns:
            Dict with keys ``"M15"``, ``"H1"``, ``"D1"`` mapping to
            DataFrames, or ``None`` if any fetch fails.
        """
        # D1 needs >=200 bars so EMA-200 can be computed; 260 gives a
        # year of trading days plus headroom for warm-up.
        m15 = self.get_candles(symbol, "15min", outputsize=200)
        h1 = self.get_candles(symbol, "1h", outputsize=250)
        d1 = self.get_candles(symbol, "1day", outputsize=260)

        if m15 is None or h1 is None or d1 is None:
            logger.error(
                "Multi-timeframe fetch incomplete for %s (m15=%s, h1=%s, d1=%s)",
                symbol, m15 is not None, h1 is not None, d1 is not None,
            )
            return None
        return {"M15": m15, "H1": h1, "D1": d1}

    def get_dxy_context(self) -> Optional[dict]:
        """Fetch DXY H1 + D1 candles for cross-asset context.

        Returns:
            Dict with keys ``"symbol"``, ``"H1"``, ``"D1"``, or
            ``None`` if DXY is unavailable. Caller should continue
            without DXY context in that case.
        """
        h1 = self.get_candles("DXY", "1h", outputsize=250)
        d1 = self.get_candles("DXY", "1day", outputsize=260)
        if h1 is None or d1 is None:
            logger.warning("DXY context unavailable; continuing without it")
            return None
        return {"symbol": "DXY", "H1": h1, "D1": d1}
