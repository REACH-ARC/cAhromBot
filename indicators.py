"""Technical indicator computation for OHLCV candle data.

Uses pandas-ta to compute RSI, EMA-50, EMA-200, MACD, and ATR on a
DataFrame of OHLCV candles, plus simple support/resistance levels and
a coarse trend bias label. All values are rounded for downstream
prompt-friendly use; any indicator that cannot be computed (insufficient
history) is reported as None rather than raising.
"""

from __future__ import annotations

import logging
import math
from typing import Optional

import pandas as pd
import pandas_ta as ta

logger = logging.getLogger(__name__)


_ATR_MAX_FRAC_OF_PRICE = 0.05  # ATR > 5% of price ⇒ corrupt data, reject cycle.
_RECENT_LOOKBACK = 20


def _safe_round(value: object, ndigits: int = 2) -> Optional[float]:
    """Round a numeric value, returning None for NaN/missing input.

    Args:
        value: Value to round (may be NaN, None, or numeric).
        ndigits: Number of decimal places.

    Returns:
        Rounded float, or None if the input is missing/NaN.
    """
    if value is None:
        return None
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(f):
        return None
    return round(f, ndigits)


def compute_indicators(df: pd.DataFrame) -> dict:
    """Compute the indicator bundle for a single timeframe.

    Args:
        df: OHLCV DataFrame with at least ``open, high, low, close``.

    Returns:
        Flat dict of indicator values. Keys with insufficient data
        return ``None`` rather than NaN. Always includes a ``close``
        and ``trend_bias`` key.
    """
    if df is None or df.empty:
        logger.error("compute_indicators received empty DataFrame")
        return {}

    work = df.copy()
    close = work["close"]
    high = work["high"]
    low = work["low"]

    rsi = ta.rsi(close, length=14)
    ema50 = ta.ema(close, length=50)
    ema200 = ta.ema(close, length=200)
    macd = ta.macd(close, fast=12, slow=26, signal=9)
    atr = ta.atr(high=high, low=low, close=close, length=14)
    adx_df = ta.adx(high=high, low=low, close=close, length=14)

    last_close = _safe_round(close.iloc[-1] if len(close) else None, 2)
    last_rsi = _safe_round(rsi.iloc[-1] if rsi is not None and len(rsi) else None, 2)
    last_ema50 = _safe_round(ema50.iloc[-1] if ema50 is not None and len(ema50) else None, 2)
    last_ema200 = _safe_round(ema200.iloc[-1] if ema200 is not None and len(ema200) else None, 2)
    last_atr = _safe_round(atr.iloc[-1] if atr is not None and len(atr) else None, 2)

    macd_line: Optional[float] = None
    macd_signal: Optional[float] = None
    macd_hist: Optional[float] = None
    if macd is not None and not macd.empty:
        try:
            macd_line = _safe_round(macd["MACD_12_26_9"].iloc[-1], 4)
            macd_signal = _safe_round(macd["MACDs_12_26_9"].iloc[-1], 4)
            macd_hist = _safe_round(macd["MACDh_12_26_9"].iloc[-1], 4)
        except KeyError as exc:
            logger.warning("MACD column missing in pandas-ta output: %s", exc)

    adx_value: Optional[float] = None
    di_plus: Optional[float] = None
    di_minus: Optional[float] = None
    if adx_df is not None and not adx_df.empty:
        try:
            adx_value = _safe_round(adx_df["ADX_14"].iloc[-1], 2)
            di_plus = _safe_round(adx_df["DMP_14"].iloc[-1], 2)
            di_minus = _safe_round(adx_df["DMN_14"].iloc[-1], 2)
        except KeyError as exc:
            logger.warning("ADX columns missing in pandas-ta output: %s", exc)

    if adx_value is None:
        trend_strength = None
    elif adx_value >= 25:
        trend_strength = "trending"
    elif adx_value < 20:
        trend_strength = "ranging"
    else:
        trend_strength = "weak"

    # Reject cycles where ATR is implausibly large vs. price — almost
    # always a yfinance payload glitch (sparse/spliced bars). Better to
    # skip than feed corrupt context to the analyst.
    if (
        last_atr is not None
        and last_close is not None
        and last_close > 0
        and last_atr > last_close * _ATR_MAX_FRAC_OF_PRICE
    ):
        logger.error(
            "Suspect ATR=%.2f vs close=%.2f (>%.0f%%); skipping cycle",
            last_atr, last_close, _ATR_MAX_FRAC_OF_PRICE * 100,
        )
        return {}

    recent = work.tail(_RECENT_LOOKBACK)
    if (
        recent.empty
        or len(recent) < _RECENT_LOOKBACK
        or recent["high"].isna().all()
        or recent["low"].isna().all()
    ):
        logger.warning(
            "Recent %d-bar slice incomplete (rows=%d); recent_high/low set to None",
            _RECENT_LOOKBACK, len(recent),
        )
        recent_high = None
        recent_low = None
    else:
        recent_high = _safe_round(recent["high"].max(), 2)
        recent_low = _safe_round(recent["low"].min(), 2)
        # Guard against a min/max of exactly 0 (or NaN passing _safe_round)
        # which historically appeared in logs after sparse fetches.
        if recent_low is not None and recent_low <= 0:
            logger.warning("Recent low computed as <=0 (%.2f); coercing to None", recent_low)
            recent_low = None
        if recent_high is not None and recent_high <= 0:
            logger.warning("Recent high computed as <=0 (%.2f); coercing to None", recent_high)
            recent_high = None

    if last_close is None or last_ema200 is None:
        price_position: Optional[str] = None
    else:
        price_position = "above_ema200" if last_close > last_ema200 else "below_ema200"

    if None in (last_close, last_ema50, last_ema200):
        trend_bias = "neutral"
    elif last_close > last_ema50 > last_ema200:
        trend_bias = "bullish"
    elif last_close < last_ema50 < last_ema200:
        trend_bias = "bearish"
    else:
        trend_bias = "neutral"

    return {
        "close": last_close,
        "rsi": last_rsi,
        "ema50": last_ema50,
        "ema200": last_ema200,
        "macd": macd_line,
        "macd_signal": macd_signal,
        "macd_hist": macd_hist,
        "atr": last_atr,
        "adx": adx_value,
        "di_plus": di_plus,
        "di_minus": di_minus,
        "trend_strength": trend_strength,
        "recent_high_20": recent_high,
        "recent_low_20": recent_low,
        "price_position": price_position,
        "trend_bias": trend_bias,
    }
