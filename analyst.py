"""Claude-powered XAUUSD signal analyst.

Builds a structured prompt from M15 and H1 indicator snapshots, sends
it to the Anthropic API, and parses the model's JSON response into a
signal dict. All errors (network, parsing, malformed output) are
caught and logged; the function returns None on failure so that the
main loop can simply skip a cycle rather than crash.
"""

from __future__ import annotations

import json
import logging
import re
import time
from typing import Optional

from anthropic import Anthropic

from config import ANTHROPIC_API_KEY

logger = logging.getLogger(__name__)

_MODEL = "claude-opus-4-5"
_MAX_TOKENS = 600
_RETRY_ATTEMPTS = 3
_RETRY_BACKOFF_SECONDS = (2, 5, 10)

_SYSTEM_PROMPT = (
    "You are a professional XAUUSD (gold/USD) technical analyst. "
    "You produce concise, disciplined, risk-aware trade signals based "
    "strictly on the indicator snapshots provided. You never invent data "
    "and you respond with valid JSON only — no prose, no markdown fences."
)

_REQUIRED_KEYS = {
    "signal",
    "confidence",
    "entry_zone",
    "stop_loss",
    "take_profit",
    "reasoning",
    "timeframe_bias",
}

_client = Anthropic(api_key=ANTHROPIC_API_KEY)


def _build_user_prompt(
    context_m15: dict,
    context_h1: dict,
    context_d1: Optional[dict] = None,
    dxy_context: Optional[dict] = None,
) -> str:
    """Render the indicator context into the user-facing prompt body.

    Args:
        context_m15: Indicator dict for the 15-minute timeframe.
        context_h1: Indicator dict for the 1-hour timeframe.
        context_d1: Indicator dict for the daily timeframe (optional).
        dxy_context: Dict with ``H1`` and ``D1`` indicator dicts for DXY
            (US Dollar Index) plus a ``symbol`` key. Optional — pass
            ``None`` if DXY data is not available this cycle.

    Returns:
        The text prompt to send to Claude.
    """
    sections = [
        f"M15 snapshot:\n{json.dumps(context_m15, indent=2)}",
        f"H1 snapshot:\n{json.dumps(context_h1, indent=2)}",
    ]
    if context_d1 is not None:
        sections.append(f"D1 snapshot:\n{json.dumps(context_d1, indent=2)}")

    if dxy_context is not None:
        dxy_block = {
            "symbol": dxy_context.get("symbol", "DXY"),
            "H1": dxy_context.get("H1"),
            "D1": dxy_context.get("D1"),
        }
        sections.append(
            "DXY (US Dollar Index) cross-asset context — gold has a "
            "strong inverse correlation with DXY:\n"
            f"{json.dumps(dxy_block, indent=2)}"
        )

    body = "\n\n".join(sections)
    return (
        "Analyze the following XAUUSD indicator snapshots and output a "
        "trade signal as a single JSON object.\n\n"
        f"{body}\n\n"
        "Return ONLY a JSON object with EXACTLY these keys:\n"
        "  signal         -> one of \"BUY\", \"SELL\", \"WAIT\"\n"
        "  confidence     -> integer 0-100\n"
        "  entry_zone     -> string like \"2345.00-2348.00\"\n"
        "  stop_loss      -> string price level\n"
        "  take_profit    -> string price level\n"
        "  reasoning      -> max 2 sentences\n"
        "  timeframe_bias -> short string describing M15/H1/D1 alignment\n\n"
        "Rules:\n"
        "- Daily (D1) bias is the dominant context: counter-trend trades "
        "vs. D1 should reduce confidence or become WAIT.\n"
        "- DXY trend (when present) is inversely correlated with gold; "
        "BUY gold setups against a strongly bullish DXY should reduce "
        "confidence; SELL gold setups against bearish DXY likewise.\n"
        "- ADX < 20 = ranging market (mean-reversion plays preferred); "
        "ADX > 25 = trending (trend-continuation preferred). Treat "
        "EMA-aligned trends as suspect when ADX is weak.\n"
        "- Use WAIT when timeframes disagree or signals are weak.\n"
        "- Stop loss must respect ATR (typically 1.5-2x ATR beyond "
        "structure). Take profit MUST yield reward:risk >= 2.0 — "
        "compute (TP-entry)/(entry-SL) for BUY or (entry-TP)/(SL-entry) "
        "for SELL before responding. If the nearest realistic TP target "
        "given recent structure cannot reach 2R, return WAIT rather "
        "than a sub-2R BUY/SELL; do not stretch TP to fake the ratio.\n"
        "- No text outside the JSON object."
    )


def _extract_json(raw: str) -> Optional[dict]:
    """Best-effort extraction of a JSON object from a model response.

    Args:
        raw: Raw string returned by Claude.

    Returns:
        Parsed dict, or None if no valid JSON object can be extracted.
    """
    if not raw:
        return None

    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        pass

    match = re.search(r"\{.*\}", raw, re.DOTALL)
    if not match:
        return None
    try:
        return json.loads(match.group(0))
    except json.JSONDecodeError:
        return None


def analyze(
    context_m15: dict,
    context_h1: dict,
    context_d1: Optional[dict] = None,
    dxy_context: Optional[dict] = None,
) -> Optional[dict]:
    """Run the Claude analyst on the supplied indicator context.

    Args:
        context_m15: Indicator dict from indicators.compute_indicators
            for the 15-minute timeframe.
        context_h1: Indicator dict for the 1-hour timeframe.
        context_d1: Indicator dict for the daily timeframe. Optional;
            pass ``None`` to omit D1 context (e.g. if the D1 fetch failed).
        dxy_context: Dict with H1/D1 indicator dicts for DXY plus the
            resolved Twelve Data symbol. Optional.

    Returns:
        Parsed signal dict on success, or None if the call fails or
        the response cannot be parsed into the expected schema.
    """
    user_prompt = _build_user_prompt(context_m15, context_h1, context_d1, dxy_context)

    message = None
    last_exc: Optional[Exception] = None
    for attempt in range(_RETRY_ATTEMPTS):
        try:
            message = _client.messages.create(
                model=_MODEL,
                max_tokens=_MAX_TOKENS,
                system=_SYSTEM_PROMPT,
                messages=[{"role": "user", "content": user_prompt}],
            )
            break
        except Exception as exc:  # noqa: BLE001 - any SDK/network error
            last_exc = exc
            logger.warning(
                "Anthropic attempt %d/%d failed: %s",
                attempt + 1, _RETRY_ATTEMPTS, exc,
            )
            if attempt < _RETRY_ATTEMPTS - 1:
                time.sleep(_RETRY_BACKOFF_SECONDS[attempt])

    if message is None:
        logger.error("Anthropic API call exhausted retries: %s", last_exc)
        return None

    try:
        raw_text = "".join(
            block.text for block in message.content if getattr(block, "type", "") == "text"
        )
    except Exception as exc:  # noqa: BLE001
        logger.error("Failed to extract text from Claude response: %s", exc)
        return None

    parsed = _extract_json(raw_text)
    if parsed is None:
        logger.error("Could not parse JSON from Claude response. Raw: %r", raw_text)
        return None

    missing = _REQUIRED_KEYS - parsed.keys()
    if missing:
        logger.error("Claude response missing required keys %s. Raw: %r", missing, raw_text)
        return None

    signal_value = str(parsed.get("signal", "")).upper().strip()
    if signal_value not in {"BUY", "SELL", "WAIT"}:
        logger.error("Claude returned invalid signal value %r. Raw: %r", signal_value, raw_text)
        return None
    parsed["signal"] = signal_value

    try:
        parsed["confidence"] = int(parsed["confidence"])
    except (TypeError, ValueError):
        logger.error("Claude confidence not int-coercible. Raw: %r", raw_text)
        return None

    return parsed
