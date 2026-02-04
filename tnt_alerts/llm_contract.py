from __future__ import annotations

import json
from typing import Any, Literal

from pydantic import BaseModel, Field

from .alert_intent import AlertIntent
from .reasons import PARSE_AMBIGUOUS, WARN_DEFAULT_TIMEFRAME_USED


class CompilerWarning(BaseModel):
    code: str
    note: str | None = None


class ClarifyPrompt(BaseModel):
    question: str
    choices: list[str]
    default: str | None = None


class LLMCompilerEnvelope(BaseModel):
    ok: bool
    intent: AlertIntent | None = None
    warnings: list[CompilerWarning] = Field(default_factory=list)
    clarify: ClarifyPrompt | None = None


def parse_llm_compiler_envelope(text: str) -> LLMCompilerEnvelope:
    """Parse and validate the strict compiler contract envelope.

    This expects the LLM to return raw JSON (no markdown) matching:
    - ok=true -> intent != null and clarify == null
    - ok=false -> intent == null and clarify != null
    """

    try:
        obj = json.loads(text)
    except Exception as exc:
        raise ValueError(f"LLM response is not valid JSON: {type(exc).__name__}: {exc}")

    env = LLMCompilerEnvelope.model_validate(obj)

    if env.ok:
        if env.intent is None:
            raise ValueError("ok=true requires intent")
        if env.clarify is not None:
            raise ValueError("ok=true requires clarify=null")
    else:
        if env.intent is not None:
            raise ValueError("ok=false requires intent=null")
        if env.clarify is None:
            raise ValueError("ok=false requires clarify")

    return env


SYSTEM_PROMPT_ALERT_COMPILER_V1 = """You are TNT Alert Compiler. Convert user requests into a STRICT JSON object that matches the AlertIntent v1 schema.

HARD RULES:
- Output MUST be valid JSON. No markdown. No comments.
- You MUST use ONLY the allowed enums and shapes listed below.
- If the user asks for unsupported features (options/OI/news/custom indicators), set ok=false and propose the closest supported alternative in clarify.
- If the direction/timeframe/session is missing, you may fill defaults and add a warning.
- Include intent.direction as one of: AUTO/BULLISH/BEARISH/NEUTRAL. If the user doesn't specify, use AUTO.
- Symbols must be uppercase. If more than 20 symbols are requested, set ok=false and clarify how to split or use a watchlist.

ALLOWED TIMEFRAMES: 1m,2m,3m,5m,10m,15m,30m,60m,1D
ALLOWED CONDITIONS:
- cross: op in {crosses_above,crosses_below}, left Series, right Series
- touch: left Series, right Level
- break: op in {breaks_above,breaks_below}, level Level
- new_extreme: kind in {new_high,new_low}, lookback_bars int
- rvol: threshold float, lookback_bars int

ALLOWED SERIES:
- price (field defaults to \"last\")
- indicator: vwap, sma(n), ema(n) where n is positive int

ALLOWED LEVELS:
- number: value float
- pivot: P,R1,R2,S1,S2
- ref: y_high,y_low,or_high(minutes),or_low(minutes)

ALLOWED GATES:
- regime: allowed list, min_confidence optional
- market_hours: session {RTH,ETH,CUSTOM} and time_window_et if CUSTOM
- macro_blackout: event_types optional, pre_minutes, post_minutes
- earnings_blackout: pre_minutes, post_minutes, confirmed_only
- news_blackout: minutes, market_minutes
- cooldown: seconds int
- max_triggers: count int
- data_freshness: price_age_seconds int

ALLOWED ACTIONS:
- discord_notify: style {compact,verbose}
- include_chart: chart {execution,context}

DEFAULTS (if missing):
- timeframe: 5m for intraday requests, 1D for 200-day SMA/new 10-day high/low style requests
- session: RTH
- confirm: close
- expiry: UNTIL EOD for intraday; UNTIL 30d for daily alerts
- actions: discord_notify(compact)

Return envelope:
{ \"ok\": boolean, \"intent\": AlertIntent|null, \"warnings\": [], \"clarify\": object|null }

AlertIntent v1 REQUIRED SHAPE (when ok=true):
{\n  \"version\": \"1.0\",\n  \"source\": {\"user_id\": \"<string>\", \"channel_id\": \"<string>\", \"request_text\": \"<original user request>\", \"created_at_utc\": \"<ISO-8601 datetime>\"},\n  \"targets\": {\"type\": \"symbols\"|\"watchlist\", \"symbols\": [\"SPY\"], \"watchlist\": null|\"<name>\", \"max_symbols\": 20},\n  \"condition\": { ... },\n  \"gates\": { ... },\n  \"lifecycle\": {\"start\": \"now\", \"expires\": null|{\"type\":\"relative\",\"hours\":8}|{\"type\":\"date\",\"date\":\"YYYY-MM-DD\"}},\n  \"actions\": [{\"type\": \"discord_notify\", \"style\": \"compact\"}],\n  \"tags\": {}\n}

IMPORTANT:
- \"condition\" is a SINGLE object (NOT an array of conditions).
- \"gates\" is an object keyed by gate name (NOT a list).
\nExample ok=false (clarify):
{\n  "ok": false,\n  "intent": null,\n  "warnings": [{"code": "PARSE_AMBIGUOUS", "note": "Direction not specified"}],\n  "clarify": {\n    "question": "Do you mean crosses ABOVE VWAP, BELOW VWAP, or BOTH?",\n    "choices": ["ABOVE", "BELOW", "BOTH"],\n    "default": "BOTH"\n  }\n}
"""
