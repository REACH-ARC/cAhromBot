"""End-to-end smoke test for xaubot.

Exercises each external dependency once: Twelve Data candles,
indicator math, the Claude analyst, and Telegram delivery. Prints
per-step PASS/FAIL with helpful diagnostic detail. Intended to be
run after installing requirements and filling .env, before deploying.
"""

from __future__ import annotations

import json
import sys
import traceback
from datetime import datetime, timezone


def step(name: str) -> None:
    print(f"\n=== {name} ===")


def ok(msg: str) -> None:
    print(f"  PASS: {msg}")


def fail(msg: str) -> None:
    print(f"  FAIL: {msg}")


def main() -> int:
    failures = 0

    step("1. config import + env validation")
    try:
        import config

        for key in ("ANTHROPIC_API_KEY", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"):
            val = getattr(config, key)
            assert val and len(val) > 5, f"{key} looks empty"
        ok(f"symbol={config.SYMBOL} confidence_min={config.CONFIDENCE_MIN}")
    except SystemExit:
        fail("config.py exited — a required env var is missing")
        return 1
    except Exception as exc:
        fail(f"config import error: {exc}")
        traceback.print_exc()
        return 1

    step("2. Market data — fetch M15 candles")
    try:
        from data import MarketDataClient

        client = MarketDataClient()
        df_m15 = client.get_candles(config.SYMBOL, "15min", outputsize=200)
        if df_m15 is None:
            fail("get_candles returned None — see log above")
            failures += 1
        else:
            assert list(df_m15.columns) == ["time", "open", "high", "low", "close", "volume"]
            assert len(df_m15) > 50, f"only {len(df_m15)} candles"
            last_close = float(df_m15["close"].iloc[-1])
            last_time = df_m15["time"].iloc[-1]
            ok(f"{len(df_m15)} M15 candles, latest @ {last_time} close={last_close}")
    except Exception as exc:
        fail(f"twelve data error: {exc}")
        traceback.print_exc()
        failures += 1
        return failures

    step("3. Market data — fetch H1 + D1 candles")
    try:
        df_h1 = client.get_candles(config.SYMBOL, "1h", outputsize=200)
        if df_h1 is None:
            fail("H1 fetch returned None")
            failures += 1
            return failures
        ok(f"{len(df_h1)} H1 candles, latest close={float(df_h1['close'].iloc[-1])}")

        df_d1 = client.get_candles(config.SYMBOL, "1day", outputsize=120)
        if df_d1 is None:
            fail("D1 fetch returned None")
            failures += 1
            return failures
        ok(f"{len(df_d1)} D1 candles, latest close={float(df_d1['close'].iloc[-1])}")
    except Exception as exc:
        fail(f"H1/D1 fetch error: {exc}")
        traceback.print_exc()
        failures += 1
        return failures

    step("3b. Market data — fetch DXY context (optional)")
    try:
        dxy_raw = client.get_dxy_context()
        if dxy_raw is None:
            print("  WARN: DXY unavailable on this plan — bot will run without DXY context")
            dxy_raw = None
        else:
            ok(f"DXY ({dxy_raw['symbol']}) H1+D1 fetched, latest close={float(dxy_raw['D1']['close'].iloc[-1])}")
    except Exception as exc:
        print(f"  WARN: DXY fetch error (non-fatal): {exc}")
        dxy_raw = None

    step("4. Indicators — compute on M15 + H1 + D1 (with ADX)")
    try:
        from indicators import compute_indicators

        ind_m15 = compute_indicators(df_m15)
        ind_h1 = compute_indicators(df_h1)
        ind_d1 = compute_indicators(df_d1)

        for name, ind in (("M15", ind_m15), ("H1", ind_h1), ("D1", ind_d1)):
            missing = [k for k in ("close", "rsi", "ema50", "ema200",
                                   "macd", "atr", "adx", "trend_strength",
                                   "trend_bias") if k not in ind]
            assert not missing, f"{name} missing keys: {missing}"
            print(f"  {name}: " + json.dumps(ind, indent=2).replace("\n", "\n        "))
        ok("indicator dicts populated (incl. ADX + trend_strength)")

        if dxy_raw is not None:
            dxy_context = {
                "symbol": dxy_raw["symbol"],
                "H1": compute_indicators(dxy_raw["H1"]),
                "D1": compute_indicators(dxy_raw["D1"]),
            }
            ok(f"DXY indicators computed: H1 trend={dxy_context['H1']['trend_bias']}, "
               f"D1 trend={dxy_context['D1']['trend_bias']}")
        else:
            dxy_context = None
    except Exception as exc:
        fail(f"indicator error: {exc}")
        traceback.print_exc()
        failures += 1
        return failures

    step("5. Claude analyst — generate signal (with D1 + DXY context)")
    try:
        from analyst import analyze

        signal = analyze(ind_m15, ind_h1, ind_d1, dxy_context)
        if signal is None:
            fail("analyze() returned None — check logs above")
            failures += 1
        else:
            print(f"  signal: {json.dumps(signal, indent=2)}")
            for key in ("signal", "confidence", "entry_zone", "stop_loss",
                        "take_profit", "reasoning", "timeframe_bias"):
                assert key in signal, f"missing key {key}"
            ok(f"{signal['signal']} @ confidence={signal['confidence']}")
    except Exception as exc:
        fail(f"analyst error: {exc}")
        traceback.print_exc()
        failures += 1
        signal = None

    step("6. Telegram — send test message")
    try:
        from notifier import send_error_alert, send_signal

        ts = datetime.now(timezone.utc).strftime("%H:%M:%S UTC")
        send_error_alert(f"smoke_test ping @ {ts} — if you see this, Telegram works")
        ok("startup-style alert dispatched (check your Telegram chat)")

        if signal is not None:
            current_price = ind_m15.get("close")
            sent = send_signal(signal, current_price)
            if sent:
                ok(f"signal-style message dispatched ({signal['signal']} @ {current_price})")
            else:
                fail("send_signal returned False")
                failures += 1
    except Exception as exc:
        fail(f"telegram error: {exc}")
        traceback.print_exc()
        failures += 1

    print("\n=========================================")
    if failures == 0:
        print("ALL CHECKS PASSED — bot is ready to schedule")
    else:
        print(f"{failures} CHECK(S) FAILED — fix above before scheduling")
    print("=========================================")
    return failures


if __name__ == "__main__":
    sys.exit(main())
