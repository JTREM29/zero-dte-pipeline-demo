from __future__ import annotations

import os
import re
import time
from dataclasses import dataclass
from enum import Enum
from typing import Any, Mapping


class Intent(str, Enum):
    TRADE_ACTION = "trade_action"  # strike/expiry/timing/sizing/trade selection
    RISK_STRUCTURE = "risk_structure"  # risk framing, structure selection heuristics
    MARKET_CONTEXT = "market_context"  # risk-on/off, posture/regime
    DATA_DIAGNOSTIC = "data_diagnostic"  # why stale/down
    OTHER = "other"


class DataState(str, Enum):
    OK = "ok"
    FUTURES_ONLY = "futures_only"
    DOWN = "down"


@dataclass(frozen=True)
class FuturesSnapshot:
    ok: bool
    trend: str | None = None  # RISK-ON | RISK-OFF | MIXED
    mode: str | None = None  # trend | chop | transition
    divergence_level: str | None = None  # aligned | mixed | divergent
    divergence: float | None = None
    age_s: int | None = None
    ingest_state: str | None = None  # OK|AGING|DOWN
    ingest_reason: str | None = None


@dataclass(frozen=True)
class Freshness:
    market_age_s: int | None
    symbol_age_s: int | None


@dataclass(frozen=True)
class Decision:
    intent: Intent
    state: DataState
    confidence: str
    reply: str
    reason: str


# --- Intent detection (deterministic) ---

# Trade-action triggers: keep these narrowly focused so "risk" questions do not get misbucketed.
_TRADE_RE = re.compile(
    r"\b(what\s+strike|which\s+strike|strike\b|expiry|expiration|exp\b|0\s*dte|0dte|1\s*dte|1dte|"
    r"how\s+many\b|contracts\b|position\s+size|size\b|sizing\b|"
    r"should\s+i\s+buy|what\s+should\s+i\s+buy|buy\b|sell\b|"
    r"stop\b|take\s+profit|tp\b|stop\s*loss|sl\b)\b",
    re.I,
)

_RISK_RE = re.compile(
    r"\b(risk\b|downside\b|gap\s+down|overnight\b|breaks?\b|break\s+down|liquidity\b|"
    r"0\s*dte\s+vs\s+1\s*dte|0dte\s+vs\s+1dte|defined\s*-?risk|spread\b|naked\b)\b",
    re.I,
)

_MARKET_CTX_RE = re.compile(
    r"\b(futures\b|risk[-\s]?on|risk[-\s]?off|regime\b|posture\b|how\s+do\s+futures\s+look|"
    r"market\s+context|how\s+does\s+the\s+market\s+look)\b",
    re.I,
)

_DIAG_RE = re.compile(
    r"\b(why\s+is\s+data\s+stale|data\s+stale|stale\b|snapshot\s+missing|"
    r"why\s+is\s+futures\s+down|futures\s+down|not\s+updating|unavailable\b)\b",
    re.I,
)


def classify_intent(text: str) -> Intent:
    t = str(text or "").strip()
    if not t:
        return Intent.OTHER

    # Priority is safety-first: if trade-action is present, block specifics.
    if _TRADE_RE.search(t):
        return Intent.TRADE_ACTION
    if _DIAG_RE.search(t):
        return Intent.DATA_DIAGNOSTIC
    if _RISK_RE.search(t):
        return Intent.RISK_STRUCTURE
    if _MARKET_CTX_RE.search(t):
        return Intent.MARKET_CONTEXT
    return Intent.OTHER


def _env_int(name: str, default: int) -> int:
    try:
        return int(str(os.getenv(name, str(default)) or str(default)).strip())
    except Exception:
        return int(default)


def _age_s(ts_utc: Any, *, now_s: int) -> int | None:
    try:
        if ts_utc is None:
            return None
        ts_i = int(ts_utc)
        if ts_i <= 0:
            return None
        return max(0, int(now_s - ts_i))
    except Exception:
        return None


def _futures_snapshot(*, redis_client: Any) -> FuturesSnapshot:
    """Best-effort futures posture + ingest health.

    This intentionally never throws; callers must treat this as best-effort.
    """

    try:
        from services.futures.futures_context_line import compute_context
        from services.futures.futures_store import FuturesStore
        from services.observability.futures_ingest_health import classify_futures_ingest

        now = int(time.time())
        store = FuturesStore(redis_client) if redis_client is not None else FuturesStore()
        hb_ts, hb_msg = store.get_heartbeat()
        status = store.get_status() or {}
        scores = store.get_scores()

        hb_age_s: int | None = None
        if hb_ts is not None:
            hb_age_s = max(0, now - int(hb_ts))

        scores_present = scores is not None
        scores_age_s: int | None = None
        if scores is not None:
            try:
                scores_age_s = max(0, now - int(scores.updated_utc))
            except Exception:
                scores_age_s = None

        hb_max_age = _env_int("TNT_FUTURES_INGEST_HB_MAX_AGE_SEC", 90)
        scores_max_age = _env_int("TNT_FUTURES_INGEST_SCORES_MAX_AGE_SEC", 120)

        ingest_state, ingest_reason = classify_futures_ingest(
            hb_age_s=hb_age_s,
            scores_age_s=scores_age_s,
            scores_present=bool(scores_present),
            note=str(status.get("note") or ""),
            hb_msg=hb_msg,
            hb_max_age=int(hb_max_age),
            scores_max_age=int(scores_max_age),
        )

        ctx = compute_context(scores, now_epoch=now, max_age_sec=int(scores_max_age))
        if ctx.state == "MISSING":
            return FuturesSnapshot(ok=False, ingest_state=ingest_state, ingest_reason=ingest_reason)

        return FuturesSnapshot(
            ok=(ctx.state == "OK"),
            trend=str(ctx.trend or "").strip() or None,
            mode=str(ctx.mode or "").strip() or None,
            divergence_level=str(ctx.divergence_level or "").strip() or None,
            divergence=float(ctx.divergence) if isinstance(ctx.divergence, (int, float)) else None,
            age_s=int(ctx.age_sec) if isinstance(ctx.age_sec, int) else None,
            ingest_state=str(ingest_state or "").strip() or None,
            ingest_reason=str(ingest_reason or "").strip() or None,
        )
    except Exception:
        return FuturesSnapshot(ok=False)


def _compute_freshness(
    *,
    ctx_market: Mapping[str, Any] | None,
    ctx_sym: Mapping[str, Any] | None,
    now_s: int,
) -> Freshness:
    m_age = _age_s(ctx_market.get("ts_utc") if isinstance(ctx_market, Mapping) else None, now_s=now_s)
    s_age = _age_s(ctx_sym.get("ts_utc") if isinstance(ctx_sym, Mapping) else None, now_s=now_s)
    return Freshness(market_age_s=m_age, symbol_age_s=s_age)


def decide_reply(
    *,
    question: str,
    symbol: str | None,
    ctx_market: Mapping[str, Any] | None,
    ctx_sym: Mapping[str, Any] | None,
    redis_client: Any,
) -> Decision | None:
    """Single authoritative moderator gate.

    Returns a deterministic reply for high-risk or data-dependent intents.
    Returns None to allow downstream handlers for other intents.
    """

    intent = classify_intent(question)
    if intent == Intent.OTHER:
        return None

    now_s = int(time.time())
    max_market_age = _env_int("TNT_CTX_MARKET_MAX_AGE_SEC", 180)
    max_symbol_age = _env_int("TNT_CTX_SYMBOL_MAX_AGE_SEC", 180)

    freshness = _compute_freshness(ctx_market=ctx_market, ctx_sym=ctx_sym, now_s=now_s)
    market_fresh = freshness.market_age_s is not None and freshness.market_age_s <= int(max_market_age)

    # Only treat symbol as required if the user asked about a symbol (or one is resolved).
    needs_symbol = bool(symbol) and intent in {Intent.TRADE_ACTION}
    symbol_fresh = (
        not needs_symbol
        or (freshness.symbol_age_s is not None and freshness.symbol_age_s <= int(max_symbol_age) and isinstance(ctx_sym, Mapping))
    )

    fut = _futures_snapshot(redis_client=redis_client)
    futures_fresh = bool(fut.ok) and (fut.age_s is None or fut.age_s <= _env_int("FUTURES_CONTEXT_MAX_AGE_SEC", 120))

    if market_fresh and symbol_fresh:
        state = DataState.OK
    elif futures_fresh:
        state = DataState.FUTURES_ONLY
    else:
        state = DataState.DOWN

    sym_u = (symbol or "").strip().upper() or "(symbol)"

    # --- Render templates ---
    if intent == Intent.TRADE_ACTION:
        if state == DataState.OK:
            reply = (
                "I can’t tell you to buy/sell or pick strikes/expirations. I *can* help with a disciplined risk/structure frame.\n\n"
                "Decision frame:\n"
                "• If conviction is low → standing aside is the correct trade.\n"
                "• Prefer defined-risk structures over open-ended exposure.\n"
                "• If you can’t monitor closely → reduce leverage (longer time, defined-risk).\n\n"
                f"Context: market age {freshness.market_age_s}s | {sym_u} age {freshness.symbol_age_s}s.\n"
                "Simulation only. Not financial advice."
            )
            return Decision(intent=intent, state=state, confidence="MED", reply=reply, reason="trade_action_ok")

        if state == DataState.FUTURES_ONLY:
            fut_line = "n/a"
            if fut.trend and fut.mode:
                div_txt = "n/a"
                if isinstance(fut.divergence, (int, float)):
                    div_txt = f"{float(fut.divergence):.2f}"
                fut_line = f"{fut.trend} | {fut.mode} | Δ={div_txt} ({fut.divergence_level or 'n/a'})"
            age_txt = f"{fut.age_s}s" if fut.age_s is not None else "n/a"

            sym_age_txt = "n/a"
            if freshness.symbol_age_s is not None:
                sym_age_txt = f"{int(freshness.symbol_age_s)}s"

            market_age_txt = "n/a"
            if freshness.market_age_s is not None:
                market_age_txt = f"{int(freshness.market_age_s)}s"

            # When ctx:market is fresh but the per-symbol snapshot is stale/missing, say that explicitly.
            prefer_symbol_stale_copy = bool(market_fresh) and bool(needs_symbol) and bool(not symbol_fresh)

            if prefer_symbol_stale_copy:
                reply = (
                    f"Context: symbol snapshot is stale for {sym_u} (market age {market_age_txt} | {sym_u} age {sym_age_txt}).\n"
                    "I can’t pick strikes/expirations.\n"
                    "Trade Actions — stand aside\n"
                    f"Futures posture (best-effort): {fut_line} | age={age_txt}\n\n"
                    "Bottom line: DO NOTHING\n"
                    "Triggers: Retry after symbol snapshots refresh; Bull/Bear triggers come from fresh key levels.\n"
                    f"Confidence: Low (symbol snapshot stale; futures age {age_txt}).\n"
                    "Simulation only. Not financial advice."
                )
            else:
                reply = (
                    f"Context: symbol snapshot is missing for {sym_u}.\n"
                    "I can’t pick strikes/expirations.\n"
                    "Trade Actions — stand aside\n"
                    f"Futures context (best-effort): {fut_line} | age={age_txt}\n\n"
                    "Bottom line: DO NOTHING\n"
                    "Triggers: Retry after symbol snapshots refresh; prefer defined-risk if you must participate.\n"
                    f"Confidence: Low (symbol snapshot missing; futures age {age_txt}).\n"
                    "Simulation only. Not financial advice."
                )
            return Decision(intent=intent, state=state, confidence="LOW", reply=reply, reason="trade_action_futures_only")

        reply = (
            "Context: data health is not good enough for trade actions right now.\n"
            "Trade Actions — stand aside\n\n"
            "Bottom line: DO NOTHING\n"
            "Triggers: Resume only after futures ingest is fresh and symbol snapshots are fresh.\n"
            "Confidence: Low (data health degraded).\n"
            "Simulation only. Not financial advice."
        )
        return Decision(intent=intent, state=state, confidence="LOW", reply=reply, reason="trade_action_down")

    if intent == Intent.RISK_STRUCTURE:
        if fut.ok:
            div_txt = "n/a"
            if isinstance(fut.divergence, (int, float)):
                div_txt = f"{float(fut.divergence):.2f}"
            age_txt = f"{fut.age_s}s" if fut.age_s is not None else "n/a"
            reply = (
                "Best-effort risk framing (futures-only):\n\n"
                f"Futures posture: {fut.trend or 'MIXED'} | {fut.mode or 'transition'} | Δ={div_txt} ({fut.divergence_level or 'n/a'}) | age={age_txt}\n\n"
                "If the level breaks overnight:\n"
                "• Primary risk: gap risk + momentum acceleration into thin liquidity.\n"
                "• Common follow-through: fast repricing at the open; stops slip; spreads widen.\n"
                "• What would change the posture: futures divergence resolves + trend flips back to aligned.\n\n"
                "Discipline:\n"
                "• If you can’t monitor → choose defined-risk and/or more time, or stand aside.\n"
                "• If edge isn’t clean → do nothing is correct.\n\n"
                "Bottom line: DO NOTHING\n"
                "Triggers: Use this framing only while futures posture is fresh.\n"
                "Simulation only. Not financial advice."
            )
            return Decision(intent=intent, state=state, confidence="MED", reply=reply, reason="risk_structure_futures")

        reply = (
            "General risk framing (limited data):\n"
            "• Overnight breaks increase gap risk and can invalidate intraday levels.\n"
            "• Liquidity is thinner; spreads widen; stops can slip.\n"
            "• If you can’t monitor, reduce exposure or use defined-risk.\n\n"
            "Simulation only. Not financial advice."
        )
        return Decision(intent=intent, state=state, confidence="LOW", reply=reply, reason="risk_structure_general")

    if intent == Intent.MARKET_CONTEXT:
        if fut.ok:
            div_txt = "n/a"
            if isinstance(fut.divergence, (int, float)):
                div_txt = f"{float(fut.divergence):.2f}"
            age_txt = f"{fut.age_s}s" if fut.age_s is not None else "n/a"
            reply = (
                "Futures context (best-effort):\n"
                f"• Posture: {fut.trend or 'MIXED'} | mode={fut.mode or 'transition'} | Δ={div_txt} ({fut.divergence_level or 'n/a'}) | age={age_txt}\n\n"
                "Important: ‘RISK-ON’ does not mean price is green; it means flows/impulse favor risk appetite even during red candles.\n\n"
                "What would flip it:\n"
                "• Divergence increases or the mode degrades to chop/transition.\n"
                "• Trend shifts from aligned to divergent.\n\n"
                "Bottom line: DO NOTHING\n"
                "Triggers: Re-check after futures posture updates.\n"
                "Simulation only. Not financial advice."
            )
            return Decision(intent=intent, state=state, confidence="HIGH", reply=reply, reason="market_context_futures")

        reply = (
            "Market context is limited right now (futures posture unavailable).\n\n"
            "Next step: restore futures ingest and confirm context snapshots are updating.\n"
            "Simulation only. Not financial advice."
        )
        return Decision(intent=intent, state=state, confidence="LOW", reply=reply, reason="market_context_down")

    if intent == Intent.DATA_DIAGNOSTIC:
        # Explain the most common real-world cause in this repo: ctx:sym missing.
        parts: list[str] = []
        if not isinstance(ctx_market, Mapping):
            parts.append("Market snapshot is missing.")
        if symbol and not isinstance(ctx_sym, Mapping):
            parts.append(f"{sym_u} symbol snapshot is missing.")

        if fut.ok:
            age_txt = f"{fut.age_s}s" if fut.age_s is not None else "n/a"
            parts.append(f"Futures ingest looks OK (scores age {age_txt}).")
        else:
            parts.append("Futures ingest does not look healthy from here.")

        why = " ".join(parts).strip() or "Data snapshot is missing/stale."
        reply = (
            f"{why}\n\n"
            "Most common cause: the context writer isn’t publishing ctx:sym snapshots (or is publishing an empty symbol set).\n"
            "Next step: verify the context writer task is running and that it is configured to publish your core symbols.\n"
            "Simulation only. Not financial advice."
        )
        conf = "HIGH" if fut.ok else "MED"
        return Decision(intent=intent, state=state, confidence=conf, reply=reply, reason="diagnostic")

    return None
