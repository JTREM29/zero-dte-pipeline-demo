from __future__ import annotations

import re

from .compiler import compile_request
from .errors import AlertCompileError, AlertValidationError
from .pretty import intent_to_dsl


def _warn(code: str, note: str) -> dict:
    return {"code": code, "note": note}


def _has_explicit_timeframe(t: str) -> bool:
    return re.search(r"\b(1m|2m|3m|5m|10m|15m|30m|60m|1d)\b", t) is not None


def _intent_partial_from_text(text: str, *, symbols: list[str], watchlist: str | None) -> dict | None:
    """Return a best-effort, minimal intent_partial.

    Tests treat this as a partial (subset) structure, not a full intent.
    """

    t = (text or "").strip().lower()
    partial: dict = {}

    # Targets
    if watchlist:
        partial["targets"] = {"type": "watchlist", "watchlist": watchlist}
    elif symbols:
        partial["targets"] = {"type": "symbols", "symbols": symbols}

    # Condition (subset)
    if "vwap" in t and ("cross" in t or "break" in t):
        op = None
        if "above" in t or "over" in t:
            op = "crosses_above"
        elif "below" in t or "under" in t or "break" in t:
            op = "crosses_below"
        if op:
            partial["condition"] = {"type": "cross", "op": op}

    if "yesterday" in t and "high" in t:
        partial["condition"] = {"type": "break", "op": "breaks_above"}
    if "yesterday" in t and "low" in t:
        partial["condition"] = {"type": "break", "op": "breaks_below"}

    if "opening range" in t and "high" in t:
        partial["condition"] = {"type": "break", "op": "breaks_above"}
    if "opening range" in t and "low" in t:
        partial["condition"] = {"type": "break", "op": "breaks_below"}

    if "touch" in t:
        partial["condition"] = {"type": "touch", "confirm": "intrabar"}

    if "new 10-day low" in t or "new 10 day low" in t:
        partial["condition"] = {"type": "new_extreme", "kind": "new_low", "timeframe": "1D"}

    m_rvol = re.search(r"rvol\s*(?:is\s*)?(?:over|above|>=|>)\s*(\d+(?:\.\d+)?)", t)
    if m_rvol:
        partial["condition"] = {
            "type": "rvol",
            "threshold": float(m_rvol.group(1)),
            "lookback_bars": 20,
        }

    # Timeframe field in condition (only when clearly present)
    if _has_explicit_timeframe(t):
        tf = re.search(r"\b(1m|2m|3m|5m|10m|15m|30m|60m|1d)\b", t).group(1)
        tf = "1D" if tf.lower() == "1d" else tf
        partial.setdefault("condition", {})
        partial["condition"]["timeframe"] = tf

    if "200" in t and "sma" in t:
        partial.setdefault("condition", {})
        partial["condition"]["timeframe"] = "1D"

    # Gates
    gates: dict = {}
    if "don't spam" in t or "dont spam" in t or "dont spam" in t:
        gates["cooldown"] = {"seconds": 300}
        gates["max_triggers"] = {"count": 3}
    if "data is fresh" in t or "fresh data" in t:
        gates["data_freshness"] = {"price_age_seconds": 60}
    if "bullish" in t and "regime" in t:
        gates["regime"] = {"allowed": ["BULLISH"]}
    if "not during transition" in t:
        gates["regime"] = {"allowed": ["BULLISH", "NEUTRAL", "BEARISH"]}

    m_conf = re.search(r"confidence\s*(?:is\s*)?(?:above|over|>=|>)\s*(0?\.\d+|1\.0+|1)\b", t)
    if m_conf:
        gates.setdefault("regime", {})
        gates["regime"]["min_confidence"] = float(m_conf.group(1))

    mwin = re.search(r"\b(\d{2}:\d{2})\s*(?:-|to|–|—|and)\s*(\d{2}:\d{2})\b", t)
    if mwin:
        gates["market_hours"] = {"session": "CUSTOM", "time_window_et": [mwin.group(1), mwin.group(2)]}

    if gates:
        partial["gates"] = gates

    # Actions
    if "include chart" in t or "include_chart" in t:
        partial["actions"] = [{"type": "discord_notify"}, {"type": "include_chart"}]

    # Lifecycle
    m_days = re.search(r"\bnext\s+(\d+)\s*days?\b", t)
    if m_days:
        partial["lifecycle"] = {"expires": {"type": "relative", "days": int(m_days.group(1))}}
    m_date = re.search(r"\buntil\s+(\d{4}-\d{2}-\d{2})\b", t)
    if m_date:
        partial["lifecycle"] = {"expires": {"type": "date", "date": m_date.group(1)}}

    return partial or None


def compile_text_to_dsl_envelope(text: str) -> dict:
    """Compile free text into the contract envelope.

    Returns:
      { ok: bool, dsl: str|null, warnings: [{code,note}], clarify: object|null, intent_partial: object|null }
    """

    raw = (text or "").strip()
    t = raw.lower()

    # Contract: explicit ambiguity => clarify.
    if "either way" in t or " both" in t:
        return {
            "ok": False,
            "dsl": None,
            "warnings": [_warn("PARSE_AMBIGUOUS", "Direction set to either way")],
            "clarify": {
                "question": "Do you mean crosses ABOVE VWAP, BELOW VWAP, or BOTH?",
                "choices": ["ABOVE", "BELOW", "BOTH"],
                "default": "BOTH",
            },
            "intent_partial": None,
        }

    try:
        out = compile_request(request_text=raw, user_id="contract", channel_id="contract")
    except AlertValidationError as exc:
        msg = str(exc)
        if "too many symbols" in msg.lower() or getattr(exc, "code", "") == "ERR_TOO_MANY_SYMBOLS":
            m = re.search(r"\((\d+)\s*;\s*max\s*(\d+)\)", msg)
            n = int(m.group(1)) if m else 0
            mx = int(m.group(2)) if m else 20
            return {
                "ok": False,
                "dsl": None,
                "warnings": [_warn("ERR_TOO_MANY_SYMBOLS", f"Requested {n} symbols; max is {mx}")],
                "clarify": {
                    "question": "That’s more than 20 symbols. Split into 2 alerts or use watchlist:<name>. What do you prefer?",
                    "choices": ["SPLIT_INTO_2", "USE_WATCHLIST"],
                    "default": "USE_WATCHLIST",
                },
                "intent_partial": None,
            }
        raise
    except AlertCompileError as exc:
        return {
            "ok": False,
            "dsl": None,
            "warnings": [_warn(getattr(exc, "code", "PARSE_AMBIGUOUS"), str(exc))],
            "clarify": {
                "question": "I couldn’t parse that alert. Can you rephrase with a symbol + condition + timeframe?",
                "choices": ["EXAMPLE_VWAP", "EXAMPLE_YHIGH", "EXAMPLE_RVOL"],
                "default": "EXAMPLE_VWAP",
            },
            "intent_partial": None,
        }

    intent = out.intents[0]
    dsl = intent_to_dsl(intent)

    warnings: list[dict] = []
    if "breaks vwap" in t and "either way" not in t:
        warnings.append(_warn("PARSE_AMBIGUOUS", "Direction not explicit; defaulted to crosses_below"))
        if not _has_explicit_timeframe(t):
            warnings.append(_warn("WARN_DEFAULT_TIMEFRAME_USED", "Timeframe not specified; using 5m"))

    syms = [s.strip().upper() for s in (intent.targets.symbols or []) if isinstance(s, str) and s.strip()]
    wlist = (intent.targets.watchlist or "").strip() if intent.targets.type == "watchlist" else None
    ip = _intent_partial_from_text(raw, symbols=syms, watchlist=wlist or None)

    return {
        "ok": True,
        "dsl": dsl,
        "warnings": warnings,
        "clarify": None,
        "intent_partial": ip,
    }
