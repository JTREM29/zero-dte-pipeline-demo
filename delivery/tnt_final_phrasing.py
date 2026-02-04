from __future__ import annotations

import re
import time
from typing import Any, Mapping


# TNT Final Phrasing Spec (LOCKED)
# This module is intentionally deterministic and template-driven.


ResponseType = str

GREETING: ResponseType = "GREETING"
FUTURES_OVERVIEW: ResponseType = "FUTURES_OVERVIEW"
MARKET_CONTEXT: ResponseType = "MARKET_CONTEXT"
SYMBOL_CONTEXT: ResponseType = "SYMBOL_CONTEXT"
CANDIDATE_APPROVED: ResponseType = "CANDIDATE_APPROVED"
CANDIDATE_NONE: ResponseType = "CANDIDATE_NONE"
DATA_STALE: ResponseType = "DATA_STALE"


_BANNED_WORDS_RE = re.compile(
    r"\b(signal|call|entry|prediction|guaranteed|will)\b",
    flags=re.IGNORECASE,
)

_UNKNOWN_WORD_RE = re.compile(r"\bunknown\b", flags=re.IGNORECASE)


def _require_no_ctx_leak(text: str) -> None:
    # Never leak internal Redis key names or ctx:* markers to user-facing output.
    if re.search(r"\bctx:", text or "", flags=re.IGNORECASE):
        raise ValueError("final phrasing spec violation: internal ctx leakage")


def _now_epoch() -> int:
    return int(time.time())


def _format_as_of_et(ts_epoch: int | None) -> str:
    if not isinstance(ts_epoch, int):
        ts_epoch = _now_epoch()
    try:
        from datetime import datetime
        from zoneinfo import ZoneInfo

        et = ZoneInfo("America/New_York")
        dt = datetime.fromtimestamp(int(ts_epoch), tz=et)
        return dt.strftime("%Y-%m-%d %H:%M ET")
    except Exception:
        return "(as of ET unavailable)"


def _age_s(ts_epoch: int | None, *, now: int | None = None) -> int | None:
    if not isinstance(ts_epoch, int):
        return None
    if now is None:
        now = _now_epoch()
    try:
        return max(0, int(now - int(ts_epoch)))
    except Exception:
        return None


def _confidence_label(*, base: str = "LOW", penalties: int = 0) -> str:
    base_u = (base or "LOW").strip().upper()
    if base_u not in {"HIGH", "MED", "LOW"}:
        base_u = "LOW"

    # Apply simple, deterministic penalties.
    if penalties <= 0:
        return base_u
    if base_u == "HIGH":
        return "MED" if penalties == 1 else "LOW"
    if base_u == "MED":
        return "LOW"
    return "LOW"


def _sources_present_summary(mkt: Mapping[str, Any] | None) -> str:
    if not isinstance(mkt, Mapping):
        return "n/a"
    sp = mkt.get("sources_present") if isinstance(mkt.get("sources_present"), Mapping) else {}
    fut = bool(sp.get("futures"))
    news = bool(sp.get("news"))
    parts = []
    parts.append("futures" if fut else "no-futures")
    parts.append("news" if news else "no-news")
    return ",".join(parts)


def _require_no_banned_words(text: str) -> None:
    if _BANNED_WORDS_RE.search(text or ""):
        raise ValueError("final phrasing spec violation: banned wording present")
    if _UNKNOWN_WORD_RE.search(text or ""):
        raise ValueError("final phrasing spec violation: UNKNOWN present")
    _require_no_ctx_leak(text)


def _confidence_word(label: str) -> str:
    l = (label or "LOW").strip().upper()
    return {"HIGH": "High", "MED": "Medium", "LOW": "Low"}.get(l, "Low")


def _normalize_bias(raw: object) -> str:
    s = str(raw or "").strip().upper()
    if s in {"BULL", "BULLISH"}:
        return "BULLISH"
    if s in {"BEAR", "BEARISH"}:
        return "BEARISH"
    return "NEUTRAL"


def _normalize_conviction(raw: object) -> str:
    s = str(raw or "").strip().upper()
    if s in {"HIGH"}:
        return "HIGH"
    if s in {"MED", "MEDIUM"}:
        return "MEDIUM"
    return "LOW"


def _normalize_regime(raw: object) -> str:
    s = str(raw or "").strip().upper()
    allowed = {"TREND", "COMPRESSION", "MEAN_REVERT", "MOMENTUM", "VOLATILE"}
    if s in allowed:
        return s
    if any(tok in s for tok in ("CHOP", "RANGE", "COMP")):
        return "COMPRESSION"
    if "MEAN" in s:
        return "MEAN_REVERT"
    if "MOM" in s:
        return "MOMENTUM"
    if "VOL" in s:
        return "VOLATILE"
    if "TREND" in s:
        return "TREND"
    return "COMPRESSION"


def _final_four_state(*, bias: str, regime: str, conviction: str, no_trade: bool) -> tuple[str, str, bool]:
    """Map internal posture to a user-facing 4-state regime.

    Output states:
    - BULLISH, BEARISH, BALANCED, TRANSITION
    """
    bias_u = _normalize_bias(bias)
    reg_u = _normalize_regime(regime)
    conv_u = _normalize_conviction(conviction)
    edge_weak = bool(no_trade) or bias_u == "NEUTRAL" or conv_u == "LOW"

    if reg_u == "VOLATILE":
        return "TRANSITION", "Whipsaw risk", edge_weak
    if reg_u in {"MOMENTUM"} and edge_weak:
        return "TRANSITION", "Momentum without conviction", edge_weak

    if not edge_weak and bias_u == "BULLISH" and reg_u in {"TREND", "MOMENTUM"}:
        return "BULLISH", "Directional edge", edge_weak
    if not edge_weak and bias_u == "BEARISH" and reg_u in {"TREND", "MOMENTUM"}:
        return "BEARISH", "Directional edge", edge_weak

    return "BALANCED", "No edge", edge_weak


def _format_triggers_from_pivots(pivots: Mapping[str, Any] | None) -> str:
    p = None
    s1 = None
    if isinstance(pivots, Mapping):
        p = pivots.get("P")
        s1 = pivots.get("S1")
    p_f = float(p) if isinstance(p, (int, float)) else None
    s1_f = float(s1) if isinstance(s1, (int, float)) else None

    if p_f is None:
        return "Triggers: Bull = reclaim + hold above key level; Bear = lose + hold below key level"

    bull = f"Bull = reclaim + hold above Pivot {p_f:.2f}"
    if s1_f is not None:
        bear = f"Bear = lose + hold below S1 {s1_f:.2f}"
    else:
        bear = f"Bear = lose + hold below Pivot {p_f:.2f}"
    return f"Triggers: {bull}; {bear}"


def build_market_snapshot_missing(*, ops_note: str | None = None) -> str:
    # DATA_STALE (Snapshot missing/stale)
    lines = [
        "Context: snapshot not available right now.",
        "Market Context — unavailable",
        "Bottom line: Snapshot is temporarily stale — try again in ~30 seconds.",
        "Triggers: Retry when the snapshot is fresh.",
        "Confidence: LOW (waiting for a fresh snapshot)",
    ]
    if ops_note:
        lines.append(f"Ops note: {ops_note}")

    out = "\n".join(lines).strip()
    _require_no_banned_words(out)
    return out


def build_market_best_effort_futures_only(
    *,
    futures_line: str | None,
    hb_age_sec: int | None,
    hb_msg: str | None,
    ingest_state: str | None,
    ingest_reason: str | None,
    confidence: str = "LOW",
    ops_note: str | None = None,
) -> str:
    """Fallback when ctx:market is missing.

    Contract: during live hours, respond with best-available read + explicit confidence
    and stand-aside guidance. This template stays deterministic and avoids predictions.
    """

    conf = (confidence or "LOW").strip().upper()
    if conf not in {"HIGH", "MED", "LOW"}:
        conf = "LOW"

    hb_age_txt = "n/a"
    if isinstance(hb_age_sec, int):
        hb_age_txt = f"{int(max(0, hb_age_sec))}s"

    ingest_bits = []
    if ingest_state:
        ingest_bits.append(str(ingest_state).strip().upper())
    if ingest_reason:
        ingest_bits.append(str(ingest_reason).strip())
    ingest_txt = " | ".join([b for b in ingest_bits if b]) or "n/a"

    fut_txt = (futures_line or "").strip()
    if not fut_txt:
        fut_txt = "Futures context: unavailable"

    msg = (hb_msg or "").strip()
    if msg:
        msg = msg.replace("\n", " ")

    lines = [
        "Context: market snapshot missing — using futures ingest (best-effort).",
        "Market Context — best-effort (futures-only)",
        f"- Ingest health: {ingest_txt}",
        f"- Heartbeat age: {hb_age_txt}" + (f" | Status: {msg}" if msg else ""),
        f"- {fut_txt}",
        "Bottom line: Treat this as low-stakes posture only.",
        "Triggers: Use this only once data is fresh.",
        f"Confidence: {_confidence_word(conf)} (market snapshot missing)",
    ]
    if ops_note:
        lines.append(f"Ops note: {ops_note}")

    out = "\n".join(lines).strip()
    _require_no_banned_words(out)
    return out


def build_greeting(*, mkt: Mapping[str, Any] | None) -> str:
    now = _now_epoch()
    ts_utc = mkt.get("ts_utc") if isinstance(mkt, Mapping) else None
    ctx_age = _age_s(int(ts_utc), now=now) if isinstance(ts_utc, int) else None

    penalties = 0
    if ctx_age is None:
        penalties += 2
    elif ctx_age > 180:
        penalties += 1

    sources = _sources_present_summary(mkt)
    if sources == "n/a":
        penalties += 1

    conf = _confidence_label(base="HIGH", penalties=penalties)

    freshness = "snapshots are fresh" if conf == "HIGH" else ("snapshots may be stale" if conf == "LOW" else "snapshots are mostly fresh")

    out = "\n".join(
        [
            "Context: I’m live and watching current market conditions.",
            "Greeting — ready",
            "Bottom line: Ask me about futures, the market, or a symbol anytime.",
            "Triggers: Ask a symbol (SPY/QQQ/IWM) or ask about futures.",
            f"Confidence: {_confidence_word(conf)} ({freshness}).",
        ]
    ).strip()
    _require_no_banned_words(out)
    return out


def build_futures_overview(*, mkt: Mapping[str, Any] | None) -> str:
    now = _now_epoch()
    ts_utc = mkt.get("ts_utc") if isinstance(mkt, Mapping) else None
    as_of_et = _format_as_of_et(int(ts_utc) if isinstance(ts_utc, int) else None)

    fut = mkt.get("futures") if isinstance(mkt, Mapping) and isinstance(mkt.get("futures"), Mapping) else {}

    bias = _normalize_bias(fut.get("bias"))
    regime = str(fut.get("regime") or "").strip().upper() or "COMPRESSION"
    hb_age = fut.get("hb_age_sec")
    hb_age_s = int(hb_age) if isinstance(hb_age, (int, float)) else None

    scores_updated = fut.get("updated_utc")
    scores_age_s: int | None = None
    if isinstance(scores_updated, (int, float)):
        scores_age_s = _age_s(int(scores_updated), now=now)

    scores_present = bool((mkt.get("sources_present") if isinstance(mkt, Mapping) else {}).get("futures"))
    degraded = bool(fut.get("degraded"))

    # Determine availability.
    if not scores_present or hb_age_s is None or degraded:
        status = "MISSING" if not scores_present else "STALE"
        hb_txt = "n/a" if hb_age_s is None else f"{hb_age_s}"
        out = "\n".join(
            [
                "Context: based on current market snapshot…",
                "Futures (ES/NQ/RTY) — not available with confidence right now.",
                f"Bottom line: Futures feed is {status.lower()}; I won’t claim a futures bias until it’s fresh.",
                "Triggers: Wait for a fresh futures snapshot.",
                f"Confidence: Low (hb age {hb_txt}s | scores present: {str(bool(scores_present)).lower()})",
            ]
        ).strip()
        _require_no_banned_words(out)
        return out

    # Final mapping (subscriber-grade): never emit UNKNOWN.
    state, state_note, edge_weak = _final_four_state(bias=bias, regime=regime, conviction="LOW", no_trade=False)

    translation = "Overnight conditions are balanced; directional edge is limited."
    if state == "TRANSITION":
        translation = "Conditions are shifting; follow-through is less reliable until a clean edge returns."
    elif state == "BULLISH":
        translation = "Overnight pressure is supportive; buyers are in control at key levels."
    elif state == "BEARISH":
        translation = "Overnight pressure is heavy; sellers are in control at key levels."

    # Confidence label
    penalties = 0
    if hb_age_s is not None and hb_age_s > 90:
        penalties += 1
    if scores_age_s is not None and scores_age_s > 180:
        penalties += 1

    # If the snapshot is fresh but the posture is ambiguous, downgrade to Medium.
    if edge_weak:
        penalties += 1
    conf = _confidence_label(base="HIGH", penalties=penalties)
    hb_txt = "n/a" if hb_age_s is None else f"{hb_age_s}"
    scores_txt = "n/a" if scores_age_s is None else f"{scores_age_s}"

    bottom_line = "DO NOTHING" if state in {"BALANCED", "TRANSITION"} else "SELECTIVE"
    triggers = "Triggers: Bull = reclaim + hold above key level; Bear = lose + hold below key level"

    out = "\n".join(
        [
            "Context: based on current market snapshot…",
            f"Futures (ES/NQ/RTY) — {as_of_et}",
            f"Regime: {state} ({state_note})",
            f"What it means: {translation}",
            f"Bottom line: {bottom_line}",
            triggers,
            f"Confidence: {_confidence_word(conf)} (hb age {hb_txt}s | scores {scores_txt}s)",
        ]
    ).strip()
    _require_no_banned_words(out)
    return out


def build_market_context(
    *,
    mkt: Mapping[str, Any] | None,
    tnt_state: Mapping[str, Any] | None,
) -> str:
    # MARKET_CONTEXT (Normal)
    now = _now_epoch()
    ts_utc = mkt.get("ts_utc") if isinstance(mkt, Mapping) else None
    ctx_age = _age_s(int(ts_utc), now=now) if isinstance(ts_utc, int) else None
    as_of_et = _format_as_of_et(int(ts_utc) if isinstance(ts_utc, int) else None)

    posture = tnt_state.get("posture") if isinstance(tnt_state, Mapping) and isinstance(tnt_state.get("posture"), Mapping) else {}
    market_regime = _normalize_regime(posture.get("regime"))
    market_bias = _normalize_bias(posture.get("bias"))

    # Risk State summary (tight, single line)
    crowd = tnt_state.get("crowding_risk") if isinstance(tnt_state, Mapping) and isinstance(tnt_state.get("crowding_risk"), Mapping) else {}
    narr = tnt_state.get("narrative_risk") if isinstance(tnt_state, Mapping) and isinstance(tnt_state.get("narrative_risk"), Mapping) else {}
    struct = tnt_state.get("structure_stress") if isinstance(tnt_state, Mapping) and isinstance(tnt_state.get("structure_stress"), Mapping) else {}
    crowd_r = str(crowd.get("risk") or "LOW").strip().upper() or "LOW"
    narr_r = str(narr.get("risk") or "LOW").strip().upper() or "LOW"
    struct_r = str(struct.get("risk") or "LOW").strip().upper() or "LOW"
    risk_line = f"Vol={struct_r} | Liquidity={crowd_r} | Positioning={narr_r}"

    perms = tnt_state.get("permissions") if isinstance(tnt_state, Mapping) and isinstance(tnt_state.get("permissions"), Mapping) else {}
    no_trade = bool(perms.get("no_trade"))
    conviction = _normalize_conviction(posture.get("conviction"))

    state, state_note, edge_weak = _final_four_state(
        bias=market_bias,
        regime=market_regime,
        conviction=conviction,
        no_trade=no_trade,
    )

    bottom_line = "DO NOTHING" if edge_weak or state in {"BALANCED", "TRANSITION"} else "SELECTIVE"
    triggers = "Triggers: Bull = reclaim + hold above key level; Bear = lose + hold below key level"

    # Confidence
    penalties = 0
    if ctx_age is None:
        penalties += 2
    elif ctx_age > 180:
        penalties += 1
    sources = _sources_present_summary(mkt)
    if "no-futures" in sources:
        penalties += 1

    # Fresh data + weak edge => Medium confidence (discipline output).
    if edge_weak:
        penalties += 1
    conf = _confidence_label(base="HIGH", penalties=penalties)
    ctx_txt = "n/a" if ctx_age is None else f"{ctx_age}"

    conf_note = "fresh snapshot; signal weak" if edge_weak and ctx_age is not None and ctx_age <= 180 else "snapshot quality varies"

    out = "\n".join(
        [
            "Context: based on current market snapshot…",
            f"Market Context — {as_of_et}",
            f"Risk State: {risk_line}",
            f"Regime: {state} ({state_note})",
            f"Bottom line: {bottom_line}",
            triggers,
            f"Confidence: {_confidence_word(conf)} ({conf_note}; snapshot age {ctx_txt}s | sources: {sources})",
        ]
    ).strip()
    _require_no_banned_words(out)
    return out


def _level_summary_from_state(*, tnt_state: Mapping[str, Any], symbol: str) -> str:
    price = tnt_state.get("price") if isinstance(tnt_state.get("price"), Mapping) else {}
    levels = tnt_state.get("levels") if isinstance(tnt_state.get("levels"), Mapping) else {}
    piv = levels.get("pivots_rth") if isinstance(levels.get("pivots_rth"), Mapping) else {}

    last = price.get("last")
    p = piv.get("P")
    r1 = piv.get("R1")
    s1 = piv.get("S1")

    if not isinstance(last, (int, float)) or not isinstance(p, (int, float)):
        return "Approaching a decision level."

    last_f = float(last)
    p_f = float(p)

    if abs(last_f - p_f) / max(1.0, abs(p_f)) < 0.0015:
        return "Approaching a decision level at Pivot."

    if last_f > p_f:
        if isinstance(r1, (int, float)) and last_f >= float(r1):
            return "Above pivot / reclaiming levels"
        return "Above pivot / reclaiming levels"

    if last_f < p_f:
        if isinstance(s1, (int, float)) and last_f <= float(s1):
            return "Below pivot / under resistance"
        return "Below pivot / under resistance"

    return "Approaching a decision level."


def _positioning_line_from_state(*, tnt_state: Mapping[str, Any]) -> str:
    crowd = tnt_state.get("crowding_risk") if isinstance(tnt_state.get("crowding_risk"), Mapping) else {}
    risk = str(crowd.get("risk") or "LOW").strip().upper() or "LOW"
    tags = crowd.get("tags")
    if isinstance(tags, list) and tags:
        tag = str(tags[0] or "").strip()
        if tag:
            return f"Options positioning: {risk} ({tag})"
    return f"Options positioning: {risk}"


def build_symbol_context(
    *,
    symbol: str,
    mkt: Mapping[str, Any] | None,
    sym_ctx: Mapping[str, Any] | None,
    tnt_state: Mapping[str, Any] | None,
) -> str:
    sym = (symbol or "").strip().upper() or "N/A"
    now = _now_epoch()

    mkt_ts = mkt.get("ts_utc") if isinstance(mkt, Mapping) else None
    sym_ts = sym_ctx.get("ts_utc") if isinstance(sym_ctx, Mapping) else None

    ctx_age = _age_s(int(mkt_ts), now=now) if isinstance(mkt_ts, int) else None
    sym_age = _age_s(int(sym_ts), now=now) if isinstance(sym_ts, int) else None

    # SYMBOL_CONTEXT (Symbol snapshot missing)
    if not isinstance(sym_ctx, Mapping):
        ctx_txt = "n/a" if ctx_age is None else f"{ctx_age}"

        # Futures-only posture (best-effort) so this never becomes a dead-end.
        fut = mkt.get("futures") if isinstance(mkt, Mapping) else {}
        if not isinstance(fut, Mapping):
            fut = {}
        fut_bias = _normalize_bias(fut.get("bias"))
        fut_regime = _normalize_regime(fut.get("regime"))
        hb_age = fut.get("hb_age_sec")
        hb_txt = "n/a"
        if isinstance(hb_age, (int, float)):
            try:
                hb_txt = f"{int(hb_age)}"
            except Exception:
                hb_txt = "n/a"

        out = "\n".join(
            [
                "Context: based on current market snapshot…",
                f"{sym} Snapshot — missing",
                f"Bottom line: I don’t have a fresh {sym} snapshot, so I won’t claim symbol positioning.",
                f"Futures posture: {fut_bias} | {fut_regime} (age {hb_txt}s)",
                "Triggers: Retry when the symbol snapshot is fresh.",
                f"Confidence: Low (symbol snapshot missing | market age {ctx_txt}s)",
            ]
        ).strip()
        _require_no_banned_words(out)
        return out

    ts_utc = sym_ctx.get("ts_utc") if isinstance(sym_ctx.get("ts_utc"), int) else None
    as_of_et = _format_as_of_et(int(ts_utc) if isinstance(ts_utc, int) else None)

    posture = tnt_state.get("posture") if isinstance(tnt_state, Mapping) and isinstance(tnt_state.get("posture"), Mapping) else {}
    sym_regime = _normalize_regime(posture.get("regime"))
    sym_bias = _normalize_bias(posture.get("bias"))
    conviction = _normalize_conviction(posture.get("conviction"))
    perms = tnt_state.get("permissions") if isinstance(tnt_state, Mapping) and isinstance(tnt_state.get("permissions"), Mapping) else {}
    no_trade = bool(perms.get("no_trade"))

    level_summary = _level_summary_from_state(tnt_state=tnt_state or {}, symbol=sym)
    positioning_line = _positioning_line_from_state(tnt_state=tnt_state or {})

    penalties = 0
    if sym_age is None or ctx_age is None:
        penalties += 2
    else:
        if sym_age > 180:
            penalties += 1
        if ctx_age > 180:
            penalties += 1
    state, state_note, edge_weak = _final_four_state(
        bias=sym_bias,
        regime=sym_regime,
        conviction=conviction,
        no_trade=no_trade,
    )
    if edge_weak:
        penalties += 1
    conf = _confidence_label(base="HIGH", penalties=penalties)
    sym_txt = "n/a" if sym_age is None else f"{sym_age}"
    ctx_txt = "n/a" if ctx_age is None else f"{ctx_age}"

    piv = None
    try:
        levels = tnt_state.get("levels") if isinstance(tnt_state, Mapping) else None
        piv = (levels.get("pivots_rth") if isinstance(levels, Mapping) else None)
    except Exception:
        piv = None

    triggers_line = _format_triggers_from_pivots(piv)
    bottom_line = "DO NOTHING" if edge_weak or state in {"BALANCED", "TRANSITION"} else "SELECTIVE"
    conf_note = "fresh snapshot; signal weak" if edge_weak and sym_age is not None and ctx_age is not None and sym_age <= 180 and ctx_age <= 180 else "snapshot quality varies"

    out = "\n".join(
        [
            "Context: based on current market snapshot…",
            f"{sym} Context — {as_of_et}",
            f"Price vs Key Levels: {level_summary}",
            f"Positioning: {positioning_line}",
            f"Regime: {state} ({state_note})",
            f"Bottom line: {bottom_line}",
            triggers_line,
            f"Confidence: {_confidence_word(conf)} ({conf_note}; sym age {sym_txt}s | market age {ctx_txt}s)",
        ]
    ).strip()
    _require_no_banned_words(out)
    return out


def _strategy_name(strategy: str | None) -> str:
    s = (strategy or "").strip().lower()
    return {
        "call_spread": "Call spread",
        "put_spread": "Put spread",
        "iron_condor": "Iron condor",
        "iron_fly": "Iron fly",
        "calendar": "Calendar",
        "diagonal": "Diagonal",
    }.get(s, (strategy or "Structure").strip() or "Structure")


def build_candidate_reply(*, symbol: str, sym_ctx: Mapping[str, Any] | None) -> str:
    sym = (symbol or "").strip().upper() or "N/A"

    cand = sym_ctx.get("candidates") if isinstance(sym_ctx, Mapping) and isinstance(sym_ctx.get("candidates"), Mapping) else None
    if not isinstance(cand, Mapping):
        # DATA_STALE for candidates: don't claim NONE when we don't have a fresh candidates snapshot.
        out = "\n".join(
            [
                "Context: based on current market snapshot…",
                "AI Option Structure Candidate — unavailable",
                f"Bottom line: I don’t have a fresh candidates snapshot for {sym}, so I can’t confirm a candidate.",
                "Triggers: Retry when the candidates snapshot is fresh.",
                "Confidence: Low (candidates snapshot missing)",
            ]
        ).strip()
        _require_no_banned_words(out)
        return out

    status = str(cand.get("status") or "NONE").strip().upper() or "NONE"
    as_of_et = str(cand.get("as_of_et") or "").strip() or _format_as_of_et(_now_epoch())
    age_s = cand.get("age_s")
    age_txt = "n/a" if not isinstance(age_s, int) else str(int(age_s))

    if status != "APPROVED" or not isinstance(cand.get("top"), Mapping):
        out = "\n".join(
            [
                "Context: based on current market snapshot…",
                f"AI Option Structure Candidate — {as_of_et}",
                "Bottom line: No candidate is approved right now under current conditions.",
                "Triggers: Wait for an approved candidate (status APPROVED).",
                f"Confidence: Low (candidate age {age_txt}s | status {status})",
            ]
        ).strip()
        _require_no_banned_words(out)
        return out

    top = cand.get("top") if isinstance(cand.get("top"), Mapping) else {}
    structure = _strategy_name(str(top.get("strategy") or top.get("name") or "Structure"))

    direction = str(top.get("direction") or "").strip().upper() or "NEUTRAL"
    why_lines = []
    if direction in {"BULLISH", "BEARISH", "NEUTRAL"}:
        why_lines.append(f"Bias aligns with {direction} posture")
    why_lines.append("Volatility favors defined-risk structures over naked exposure")
    why_lines.append("Positioning supports the thesis without needing perfect timing")

    # Cap at 3 bullets.
    why_lines = why_lines[:3]

    invalidation = [
        "If confirmation stays neutral",
        "If price fails to hold the key level (Pivot)",
        "If market snapshot becomes stale/degraded",
    ]

    confidence = str(cand.get("confidence") or "LOW").strip().upper() or "LOW"
    if confidence not in {"HIGH", "MED", "LOW"}:
        confidence = "LOW"

    conf_word = _confidence_word(confidence)

    out = "\n".join(
        [
            "Context: based on current market snapshot…",
            f"AI Option Structure Candidate (7–14 DTE) — {as_of_et}",
            f"Candidate: {structure} (defined-risk)",
            "Bottom line: Candidate is approved (still optional).",
            "Why it fits:",
            *[f"- {x}" for x in why_lines],
            "Important: this is a candidate, not a command.",
            "Invalidation:",
            *[f"- {x}" for x in invalidation],
            "Triggers: Use only if conditions stay aligned; invalidate on Pivot failure or stale snapshot.",
            f"Confidence: {conf_word} (candidate age {age_txt}s | status APPROVED)",
        ]
    ).strip()
    _require_no_banned_words(out)
    return out
