from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from typing import Any


# --- Hardened TNT agent contract: intent + entity extraction ---

# Hard stoplist: never treat these as symbols, even if they match the 1–5
# letters heuristic.
_SYMBOL_STOPLIST = {
    "HOW",
    "DOES",
    "WHAT",
    "WHY",
    "WHEN",
    "WHERE",
    "LOOK",
    "LIKE",
    "THIS",
    "THAT",
    "TODAY",
    "NOW",
    "PLEASE",
    "FUTURES",
    "FUTES",
}

# Lightweight allowlist for common US indices / ETFs and futures roots.
_SYMBOL_ALLOWLIST = {
    "SPY",
    "QQQ",
    "IWM",
    "SPX",
    "NDX",
    "RUT",
    "VIX",
    "ES",
    "NQ",
    "RTY",
    "YM",
    "CL",
    "GC",
    "ZN",
}


def _env_symbols_universe() -> set[str]:
    raw = (os.getenv("CTX_SNAPSHOT_SYMBOLS", "") or "").strip()
    if not raw:
        return set()
    return {s.strip().upper() for s in raw.split(",") if s.strip()}


def classify_intent(text: str | None) -> str:
    """Rule-first intent classifier.

    Returns one of:
    - GREETING
    - FUTURES_OVERVIEW
    - MARKET_CONTEXT
    - SYMBOL_CONTEXT
    - CANDIDATE_REQUEST
    - JOURNAL_REVIEW
    - HELP
    - OFFTOPIC
    """

    t = (text or "").strip().lower()
    if not t:
        return "HELP"

    # Lightweight greeting intent: if the message is only a greeting, keep the
    # response short (avoid dumping full market context).
    t_clean = re.sub(r"[^a-z\s]", " ", t)
    t_clean = re.sub(r"\s+", " ", t_clean).strip()
    if t_clean in {"hello", "hi", "hey", "gm", "good morning"}:
        return "GREETING"

    if re.search(r"\b(review my trade|journal|post[- ]trade|after trade)\b", t):
        return "JOURNAL_REVIEW"

    if re.search(r"\b(help|how to use|commands|what can you do)\b", t):
        return "HELP"

    if re.search(
        r"\b(candidate|candidates|structure|structures|spread|strangle|straddle|iron\s+condor|iron\s+fly|\b0dte\b|\bdte\b|7\s*[-–]\s*14|strike|expiry|expiration)\b",
        t,
    ):
        return "CANDIDATE_REQUEST"

    # Futures should win over symbol heuristics.
    if re.search(r"\b(fut|futes|futures)\b", t):
        return "FUTURES_OVERVIEW"

    # Market-level questions (avoid treating generic words like 'today' as market intent
    # when a symbol is present).
    if re.search(r"\b(regime|bias|what'?s going on|market context)\b", t):
        return "MARKET_CONTEXT"

    # Default: treat as symbol-context if a validated symbol exists, else market.
    return "SYMBOL_CONTEXT"


def extract_symbol_candidates(text: str | None) -> list[str]:
    """Extract possible symbols from common explicit formats.

    This is intentionally permissive; validation is done separately.
    """

    raw = (text or "").strip()
    if not raw:
        return []

    out: list[str] = []

    # $SPY
    for m in re.finditer(r"\$([A-Za-z]{1,5})\b", raw):
        out.append(m.group(1).upper())

    # (SPY)
    for m in re.finditer(r"\(([A-Za-z]{1,5})\)", raw):
        out.append(m.group(1).upper())

    # SPY:
    for m in re.finditer(r"\b([A-Za-z]{1,5})\s*:\s*", raw):
        out.append(m.group(1).upper())

    # Fallback: any 1–5 letter tokens.
    for m in re.finditer(r"\b([A-Za-z]{1,5})\b", raw):
        out.append(m.group(1).upper())

    # De-dupe, preserve order.
    seen: set[str] = set()
    uniq: list[str] = []
    for s in out:
        if s in seen:
            continue
        seen.add(s)
        uniq.append(s)
    return uniq


def is_valid_symbol(sym: str | None, *, r=None, universe: set[str] | None = None) -> bool:
    s = (sym or "").strip().upper()
    if not s:
        return False
    if s in _SYMBOL_STOPLIST:
        return False
    if s in _SYMBOL_ALLOWLIST:
        return True
    if universe is None:
        universe = _env_symbols_universe()
    if s in universe:
        return True
    # Best: exists as ctx:sym:{SYM}.
    try:
        if r is not None and hasattr(r, "exists"):
            return bool(r.exists(f"ctx:sym:{s}"))
    except Exception:
        pass
    return False


def resolve_symbol_for_text(text: str | None, *, r=None, universe: set[str] | None = None) -> str | None:
    for cand in extract_symbol_candidates(text):
        if is_valid_symbol(cand, r=r, universe=universe):
            return cand
    return None


def load_ctx_market(*, r) -> dict | None:
    try:
        return _safe_json_load(r.get("ctx:market"))
    except Exception:
        return None


def load_ctx_sym(*, r, symbol: str) -> dict | None:
    sym = (symbol or "").strip().upper()
    if not sym:
        return None
    try:
        return _safe_json_load(r.get(f"ctx:sym:{sym}"))
    except Exception:
        return None


def load_ctx_candidates(*, r, symbol: str) -> dict | None:
    sym = (symbol or "").strip().upper()
    if not sym:
        return None
    try:
        return _safe_json_load(r.get(f"ctx:sym:{sym}.candidates"))
    except Exception:
        return None


def redis_client_for_ctx():
    """Create a Redis client using TNT_* env vars.

    This mirrors the alerts/scheduler Redis env surface:
    - TNT_REDIS_HOST (default 127.0.0.1)
    - TNT_REDIS_PORT (default 6379)
    - TNT_REDIS_DB   (default 0)
    """

    from services.redis_env import redis_client

    return redis_client(timeout_s=None, decode_responses=True)


@dataclass(frozen=True)
class IntentSpec:
    intent: str
    symbol: str | None
    required_keys: list[str]
    why: str


def resolve_intent_and_required_keys(*, message_text: str, r=None, provided_symbol: str | None = None) -> IntentSpec:
    """Resolve intent, validated symbol (or None), and explicit required ctx keys.

    Key behavior:
    - Stopwords can never be symbols (enforced by is_valid_symbol).
    - Futures intent short-circuits to market-only.
    - If symbol fails validation -> symbol becomes None -> required keys remain market-only.
    """

    intent = classify_intent(message_text)
    universe = _env_symbols_universe()

    sym: str | None
    if is_valid_symbol(provided_symbol, r=r, universe=universe):
        sym = (provided_symbol or "").strip().upper()
    else:
        sym = resolve_symbol_for_text(message_text, r=r, universe=universe)

    # If we have a validated symbol, treat ambiguous market prompts as symbol-context.
    # This keeps routing stable for prompts like "does SPY look weak" even when other
    # tests or upstream logic introduces market-keyword noise.
    if intent == "MARKET_CONTEXT" and sym is not None:
        intent = "SYMBOL_CONTEXT"

    required: list[str] = []
    why = ""

    if intent in {"FUTURES_OVERVIEW", "MARKET_CONTEXT"}:
        required = ["ctx:market"]
        why = "market_only_intent"
    elif intent in {"GREETING"}:
        required = ["ctx:market"]
        why = "greeting_intent"
    elif intent in {"SYMBOL_CONTEXT", "CANDIDATE_REQUEST"}:
        required = ["ctx:market"]
        if sym:
            required.append(f"ctx:sym:{sym}")
            why = "symbol_validated"
        else:
            why = "symbol_missing_or_invalid"
    else:
        required = []
        why = "no_ctx_required"

    # Futures nuance: allow symbol as a hint but never require ctx:sym for futures.
    if intent == "FUTURES_OVERVIEW":
        required = ["ctx:market"]

    return IntentSpec(intent=intent, symbol=sym, required_keys=required, why=why)


def _safe_json_load(raw: Any) -> dict | None:
    if raw is None:
        return None
    try:
        if isinstance(raw, (bytes, bytearray)):
            raw = raw.decode("utf-8", errors="replace")
        s = str(raw)
    except Exception:
        return None
    s = s.strip()
    if not s:
        return None
    try:
        obj = json.loads(s)
        return obj if isinstance(obj, dict) else None
    except Exception:
        return None


def load_required_ctx(
    *,
    r,
    symbol: str | None = None,
    consumer: str = "coach",
    spec: IntentSpec | None = None,
    required_keys: list[str] | None = None,
) -> tuple[dict | None, dict | None, list[str]]:
    """Load required ctx keys.

    Back-compat: if spec/required_keys are omitted, loads ctx:market + ctx:sym:{SYM}.
    New: if spec is provided, loads only spec.required_keys.

    Returns (market, sym, missing[]).
    """

    if spec is None and required_keys is None:
        sym = (symbol or "").strip().upper() or "SPY"
        req = ["ctx:market", f"ctx:sym:{sym}"]
    else:
        sym = (spec.symbol if spec is not None else (symbol or "")).strip().upper() if (spec and spec.symbol) or symbol else ""
        req = list(required_keys or (spec.required_keys if spec is not None else []))

    missing: list[str] = []
    mkt: dict | None = None
    sym_ctx: dict | None = None

    for k in req:
        if k == "ctx:market":
            if not isinstance(mkt, dict):
                mkt = load_ctx_market(r=r)
            continue

        if k.startswith("ctx:sym:"):
            # IntentSpec for candidates uses ctx:sym:{SYM}; candidates are embedded
            # within that snapshot, so we do not require ctx:sym:{SYM}.candidates here.
            if ".candidates" in k:
                continue
            if not isinstance(sym_ctx, dict):
                sym2 = k.split("ctx:sym:", 1)[1].strip().upper()
                sym_ctx = load_ctx_sym(r=r, symbol=sym2)
            continue

    for k in req:
        if k == "ctx:market" and not isinstance(mkt, dict):
            missing.append("ctx:market")
        elif k.startswith("ctx:sym:") and ".candidates" not in k:
            sym2 = k.split("ctx:sym:", 1)[1].strip().upper()
            if not isinstance(sym_ctx, dict):
                missing.append(f"ctx:sym:{sym2}")

    # Best-effort miss telemetry.
    try:
        from services.context.context_miss import record_ctx_miss

        if missing:
            why = ",".join(missing)[:120]
            record_ctx_miss(r, consumer, sym=sym, why=f"missing:{why}")
    except Exception:
        pass

    return mkt, sym_ctx, missing


def load_ctx_for_question(
    *,
    r,
    question: str,
    provided_symbol: str | None = None,
    consumer: str = "coach",
) -> tuple[str, str | None, dict | None, dict | None, dict | None, list[str]]:
    """Resolve intent/symbol and load only required ctx keys.

    Returns: (intent, resolved_symbol, ctx_market, ctx_sym, ctx_candidates, missing[])
    """

    intent = classify_intent(question)
    universe = _env_symbols_universe()

    sym = None
    if is_valid_symbol(provided_symbol, r=r, universe=universe):
        sym = (provided_symbol or "").strip().upper()
    else:
        sym = resolve_symbol_for_text(question, r=r, universe=universe)

    missing: list[str] = []

    # Decide requirements by intent.
    require_market = intent in {"GREETING", "FUTURES_OVERVIEW", "MARKET_CONTEXT", "SYMBOL_CONTEXT", "CANDIDATE_REQUEST"}
    require_symbol = intent in {"SYMBOL_CONTEXT", "CANDIDATE_REQUEST"}
    require_candidates = intent in {"CANDIDATE_REQUEST"}

    # If symbol is not validated, do NOT require ctx:sym:* or candidates.
    if not sym:
        require_symbol = False
        require_candidates = False

    ctx_market = load_ctx_market(r=r) if require_market else None
    ctx_sym = load_ctx_sym(r=r, symbol=sym) if require_symbol and sym else None
    ctx_cand = load_ctx_candidates(r=r, symbol=sym) if require_candidates and sym else None

    if require_market and not isinstance(ctx_market, dict):
        missing.append("ctx:market")
    if require_symbol and sym and not isinstance(ctx_sym, dict):
        missing.append(f"ctx:sym:{sym}")
    if require_candidates and sym and not isinstance(ctx_cand, dict):
        missing.append(f"ctx:sym:{sym}.candidates")

    # Best-effort miss telemetry.
    try:
        from services.context.context_miss import record_ctx_miss

        if missing:
            why = ",".join(missing)[:120]
            record_ctx_miss(r, consumer, sym=(sym or ""), why=f"missing:{why}")
    except Exception:
        pass

    return intent, sym, ctx_market, ctx_sym, ctx_cand, missing
