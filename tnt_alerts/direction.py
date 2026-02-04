from __future__ import annotations

from enum import Enum
from typing import Any


class Direction(str, Enum):
    AUTO = "AUTO"
    BULLISH = "BULLISH"
    BEARISH = "BEARISH"
    NEUTRAL = "NEUTRAL"


def coerce_direction(raw: Any) -> str:
    """Normalize direction-like inputs to canonical v2 values.

    Canonical: AUTO|BULLISH|BEARISH|NEUTRAL
    Back-compat: LONG->BULLISH, SHORT->BEARISH
    """

    d = str(raw or "AUTO").strip().upper() or "AUTO"
    if d == "LONG":
        return "BULLISH"
    if d == "SHORT":
        return "BEARISH"
    if d in {"AUTO", "BULLISH", "BEARISH", "NEUTRAL"}:
        return d
    return "AUTO"


def _get_condition(intent: Any) -> dict[str, Any]:
    if isinstance(intent, dict):
        cond = intent.get("condition")
    else:
        cond = getattr(intent, "condition", None)
    return cond if isinstance(cond, dict) else {}


def _get_direction_raw(intent: Any) -> str:
    if isinstance(intent, dict):
        raw = intent.get("direction")
    else:
        raw = getattr(intent, "direction", None)
    return coerce_direction(raw)


def infer_direction(intent: Any) -> str:
    """Resolve intent direction into BULLISH|BEARISH|NEUTRAL.

    - If intent.direction is explicit (BULLISH/BEARISH/NEUTRAL), returns it.
    - If AUTO/missing, infers from condition semantics when possible.
    """

    d = _get_direction_raw(intent)
    if d in {"BULLISH", "BEARISH", "NEUTRAL"}:
        return d

    cond = _get_condition(intent)
    ctype = str(cond.get("type") or "").strip().lower()
    op = str(cond.get("op") or "").strip().lower()
    kind = str(cond.get("kind") or "").strip().lower()

    if ctype in {"cross", "break"}:
        if op in {"crosses_above", "breaks_above", "closes_above", "greater_than"}:
            return "BULLISH"
        if op in {"crosses_below", "breaks_below", "closes_below", "less_than"}:
            return "BEARISH"

    if ctype == "new_extreme":
        if kind in {"new_high", "higher_high"}:
            return "BULLISH"
        if kind in {"new_low", "lower_low"}:
            return "BEARISH"

    # Directionless / informational by default.
    return "NEUTRAL"


def describe_direction(intent: Any) -> str:
    """Human-friendly bias label, preserving AUTO intent.

    Example: AUTO -> "AUTO → BULLISH" once inferred.
    """

    raw = _get_direction_raw(intent)
    inferred = infer_direction(intent)
    if raw == "AUTO":
        return f"AUTO → {inferred}"
    return raw
