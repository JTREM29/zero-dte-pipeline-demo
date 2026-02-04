from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Mapping

from delivery.candidates_contract import (
    AI_TRADE_CANDIDATES_BOUNDARY_SENTENCE,
    AI_TRADE_CANDIDATES_CANONICAL_DEFINITION,
    AI_TRADE_CANDIDATES_MISINTERPRETATION_CORRECTION_SENTENCE,
    AI_TRADE_CANDIDATES_NO_CANDIDATE_SENTENCE,
)


@dataclass(frozen=True)
class CoachTokens:
    symbol: str = "SPY"
    regime: str = "UNKNOWN"
    bias: str = "NEUTRAL"
    confirm: str = "UNKNOWN"
    confidence: float | None = None
    eligibility: str = "UNKNOWN"  # ELIGIBLE | NO_TRADE | UNKNOWN
    data_health: str = "UNKNOWN"  # OK | STALE | DEGRADED | DOWN | UNKNOWN
    data_freshness_sec: int | None = None


_ENVELOPE_HEADINGS = (
    "A) State",
    "B) Why",
    "C) Action",
    "D) Risk + invalidation",
)


def _split_envelope_sections(text: str) -> dict[str, str]:
    """Parse the A/B/C/D envelope into a dict of section -> content."""

    raw = (text or "").strip()
    if not raw:
        return {}

    # Normalize headings variants (be forgiving).
    norm = raw
    norm = re.sub(r"^A\s*[\)\.]\s*state\b", "A) State", norm, flags=re.IGNORECASE | re.MULTILINE)
    norm = re.sub(r"^B\s*[\)\.]\s*why\b", "B) Why", norm, flags=re.IGNORECASE | re.MULTILINE)
    norm = re.sub(r"^C\s*[\)\.]\s*action\b", "C) Action", norm, flags=re.IGNORECASE | re.MULTILINE)
    norm = re.sub(r"^D\s*[\)\.]\s*(risk|invalidation)\b.*", "D) Risk + invalidation", norm, flags=re.IGNORECASE | re.MULTILINE)

    # Find heading positions.
    positions: list[tuple[int, str]] = []
    for h in _ENVELOPE_HEADINGS:
        m = re.search(rf"^\s*{re.escape(h)}\s*$", norm, flags=re.MULTILINE)
        if m:
            positions.append((m.start(), h))

    if len(positions) < 3:
        return {}

    positions.sort(key=lambda x: x[0])
    out: dict[str, str] = {}
    for idx, (start, heading) in enumerate(positions):
        end = positions[idx + 1][0] if idx + 1 < len(positions) else len(norm)
        block = norm[start:end].strip()
        # Remove the heading line itself.
        lines = block.splitlines()
        if lines and lines[0].strip() == heading:
            lines = lines[1:]
        out[heading] = "\n".join(lines).strip()

    return out


def normalize_coach_output(raw: str) -> tuple[str, bool]:
    """Normalize output to the A/B/C/D envelope; return (text, ok)."""

    sections = _split_envelope_sections(raw)
    if not sections:
        return ("", False)

    lines: list[str] = []
    for h in _ENVELOPE_HEADINGS:
        if h not in sections:
            # Allow missing D if others present? No.
            return ("", False)
        lines.append(h)
        body = sections[h]
        lines.append(body if body else "(n/a)")
        lines.append("")

    return ("\n".join(lines).strip(), True)


def build_observational_envelope(*, tokens: CoachTokens, missing: list[str], note: str | None = None) -> str:
    miss = ", ".join([m for m in (missing or []) if str(m).strip()])
    miss = miss or "(none)"

    freshness = "unknown"
    if tokens.data_freshness_sec is not None:
        freshness = f"age={int(tokens.data_freshness_sec)}s"

    state_line = (
        f"Regime={tokens.regime} | Bias={tokens.bias} | Confirm={tokens.confirm} | "
        f"Confidence={tokens.confidence if tokens.confidence is not None else 'n/a'} | "
        f"Eligibility={tokens.eligibility} | DataHealth={tokens.data_health} | {freshness}"
    )

    missing_set = {str(m).strip() for m in (missing or []) if str(m).strip()}

    # User-friendly, truthful templates.
    why_lines: list[str] = []
    action_lines: list[str] = []

    has_market_missing = any("market" in m.lower() for m in missing_set)
    missing_sym_keys = [m for m in missing_set if "sym" in m.lower()]
    has_redis_missing = any("redis" in m.lower() for m in missing_set)
    has_disabled = "CTX_SNAPSHOT_DISABLED" in missing_set

    if has_market_missing:
        why_lines.append("I can't access the market snapshot right now.")
        why_lines.append("I need a fresh snapshot to answer consistently.")
        action_lines.append("Try again once the snapshot loop is running.")
    elif missing_sym_keys:
        sym = tokens.symbol or "(symbol)"
        why_lines.append(f"I don't have a fresh snapshot for {sym} right now.")
        why_lines.append("I need the symbol snapshot for symbol-specific context.")
        action_lines.append("Try again in ~30 seconds, or ask about a different symbol.")
    elif has_redis_missing:
        why_lines.append("I can't reach the snapshot store right now.")
        why_lines.append("Without it I can't load the canonical snapshots.")
        action_lines.append("Verify Redis is running and TNT_REDIS_* points to the right host/port/db.")
    elif has_disabled:
        why_lines.append("Canonical ctx snapshots are disabled (CTX_SNAPSHOT_DISABLED).")
        why_lines.append("Without snapshots I can't answer consistently from the approved source-of-truth.")
        action_lines.append("Enable CTX_SNAPSHOT_ENABLED=1 and restart the bot/snapshot loop.")
    else:
        why_lines.append("I don't have the required snapshots right now.")
        action_lines.append("Refresh data and try again once snapshots are present.")

    if note:
        why_lines.append(str(note).strip())

    # Always keep the contract safe.
    action_lines.insert(0, "DO NOTHING (observational mode).")

    risk_lines = [
        "Avoid acting on stale or missing snapshots.",
        "If you want a trade anyway, provide: updated snapshot + eligibility=ELIGIBLE + confirm aligns.",
    ]

    return "\n".join(
        [
            "A) State",
            state_line,
            "",
            "B) Why",
            "\n".join(f"- {x}" for x in why_lines if x),
            "",
            "C) Action",
            "\n".join(f"- {x}" for x in action_lines if x),
            "",
            "D) Risk + invalidation",
            "\n".join(f"- {x}" for x in risk_lines if x),
        ]
    ).strip()


def apply_consistency_guard(text: str, *, tokens: CoachTokens) -> str:
    """Rule-based rewrite to prevent obvious contradictions.

    This is intentionally conservative: it only rewrites when it detects a hard
    contract violation (trade suggestion under NO_TRADE, or bullish/bearish
    contradiction without explicit countertrend framing).
    """

    out = (text or "").strip()
    if not out:
        return out

    # Ensure envelope exists; if not, leave unchanged (caller should have normalized).
    sections = _split_envelope_sections(out)
    if not sections:
        return out

    # 1) Eligibility blocks trading: force action -> DO NOTHING.
    if tokens.eligibility in {"NO_TRADE", "PROHIBITED"}:
        action = sections.get("C) Action", "")
        # Heuristic: look for entry verbs.
        if re.search(r"\b(enter|entry|buy|sell|long|short|calls?|puts?|open|add)\b", action, flags=re.IGNORECASE):
            sections["C) Action"] = "- DO NOTHING. Eligibility blocks trading right now.\n- To become eligible: confirm aligns + no_trade clears + data health OK."

    # 2) Bias contradiction: allow opposite direction only with explicit countertrend framing.
    bias = (tokens.bias or "NEUTRAL").upper()
    if bias in {"BULLISH", "BEARISH"}:
        opposite_word = "bearish" if bias == "BULLISH" else "bullish"
        action = sections.get("C) Action", "")
        new_lines: list[str] = []
        changed = False
        for ln in action.splitlines():
            if re.search(rf"\b{opposite_word}\b", ln, flags=re.IGNORECASE):
                if not re.search(r"counter\s*trend|countertrend|within\s+a\s+(bullish|bearish)\s+regime", ln, flags=re.IGNORECASE):
                    # Rewrite line to compliant framing.
                    ln2 = re.sub(rf"\b{opposite_word}\b", "countertrend", ln, flags=re.IGNORECASE)
                    ln2 = f"- Countertrend note (within {bias} regime): {ln2.lstrip('-• ').strip()}"
                    new_lines.append(ln2)
                    changed = True
                    continue
            new_lines.append(ln)
        if changed:
            sections["C) Action"] = "\n".join([l for l in new_lines if l.strip()]).strip()

    # Re-compose.
    rebuilt: list[str] = []
    for h in _ENVELOPE_HEADINGS:
        rebuilt.append(h)
        rebuilt.append((sections.get(h) or "(n/a)").strip())
        rebuilt.append("")

    return "\n".join(rebuilt).strip()


def _is_structure_recommendation_question(question: str | None) -> bool:
    q = str(question or "").strip().lower()
    if not q:
        return False
    # Conservative: only treat as a recommendation request when the user
    # is explicitly asking for trades/structures.
    keywords = (
        "candidate",
        "structure",
        "strategy",
        "spread",
        "strangle",
        "straddle",
        "iron condor",
        "iron fly",
        "debit",
        "credit",
        "call",
        "calls",
        "put",
        "puts",
        "dte",
        "0dte",
        "trade",
        "setup",
        "play",
        "what should i do",
        "recommend",
        "entry",
    )
    return any(k in q for k in keywords)


def _uses_signal_language(question: str | None) -> bool:
    q = str(question or "").strip().lower()
    if not q:
        return False
    # Terms that commonly cause users to treat candidates like directives.
    return bool(re.search(r"\b(signal|signals|entry|entries|alert|alerts|call\s*outs?|calls?|puts?|prediction|predictions)\b", q))


def _dte_bucket_text(dte: object) -> str | None:
    try:
        if dte is None:
            return None
        v = int(float(dte))
    except Exception:
        return None
    if v <= 1:
        return "0–1 DTE"
    if v <= 5:
        return "2–5 DTE"
    if v <= 14:
        return "7–14 DTE"
    return f"DTE≈{v}"


def _strategy_explain(strategy: str | None) -> list[str]:
    s = str(strategy or "").strip().lower()
    if not s:
        return []
    if "iron_condor" in s or "condor" in s:
        return [
            "Neutral / range-aware structure that benefits from time decay.",
            "Defined-risk wings can reduce tail risk versus naked options.",
        ]
    if "butterfly" in s or "fly" in s:
        return [
            "Defined-risk structure that expresses a range/target zone thesis.",
            "Useful when you expect mean reversion or pinned behavior.",
        ]
    if "short_premium" in s:
        return [
            "Premium-selling bias: prefers stable/contained movement and time decay.",
            "Risk control matters most when volatility expands.",
        ]
    if "vertical" in s or "spread" in s:
        return [
            "Defined-risk directional structure (spread) instead of naked options.",
            "Balances delta exposure with controlled downside and clearer invalidation.",
        ]
    if "trend_following" in s or "trend" in s:
        return [
            "Directional structure that expects follow-through rather than chop.",
            "Best when posture/confirmation stays aligned.",
        ]
    return ["Defined-risk option structure selected to fit current conditions."]


def _format_candidate_one_line(candidate: Mapping[str, Any]) -> str:
    # Keep this short; the full object is available in ctx for audit.
    direction = str(candidate.get("direction") or candidate.get("side") or "").strip().upper() or None
    strategy = str(candidate.get("strategy") or candidate.get("description") or candidate.get("structure") or "").strip() or None
    exp = str(candidate.get("expiration") or candidate.get("expiry") or candidate.get("exp") or "").strip() or None
    strike = candidate.get("strike")
    dte = candidate.get("target_dte") if candidate.get("target_dte") is not None else candidate.get("dte")
    score = candidate.get("score")

    parts: list[str] = []
    if direction:
        parts.append(direction)
    if strategy:
        parts.append(strategy)
    if dte is not None:
        try:
            parts.append(f"DTE≈{int(float(dte))}")
        except Exception:
            pass
    if exp:
        parts.append(f"exp={exp}")
    if strike is not None:
        try:
            parts.append(f"strike={float(strike):.0f}")
        except Exception:
            parts.append(f"strike={strike}")
    if score is not None:
        try:
            parts.append(f"score={float(score):.2f}")
        except Exception:
            pass
    return " | ".join(parts) if parts else "(candidate unavailable)"


def apply_candidate_truth_guard(
    text: str,
    *,
    candidates: Mapping[str, Any] | None,
    question: str | None = None,
) -> str:
    """Enforce that structure recommendations come from ctx:sym:{SYM}.candidates.

    Only activates for explicit structure/recommendation questions.
    """

    if not _is_structure_recommendation_question(question):
        return (text or "").strip()

    out = (text or "").strip()
    if not out:
        return out

    sections = _split_envelope_sections(out)
    if not sections:
        return out

    cand = dict(candidates or {})
    status = str(cand.get("status") or "").strip().upper()
    why = str(cand.get("why") or "").strip() or None
    eligibility = str(cand.get("eligibility") or "").strip().upper()

    top = cand.get("top") if isinstance(cand.get("top"), Mapping) else None

    # If eligibility forbids trading, treat as NONE.
    if eligibility in {"NO_TRADE", "PROHIBITED"}:
        status = "NONE"

    gentle_correction = ""
    if _uses_signal_language(question):
        gentle_correction = AI_TRADE_CANDIDATES_MISINTERPRETATION_CORRECTION_SENTENCE

    if status == "APPROVED" and isinstance(top, Mapping):
        line = _format_candidate_one_line(top)
        dte = top.get("target_dte") if top.get("target_dte") is not None else top.get("dte")
        dte_txt = _dte_bucket_text(dte)
        title = "AI Option Structure Candidate"
        if dte_txt:
            title = f"AI Option Structure Candidate ({dte_txt})"

        # B) Why: pin the canonical definition + brief fit notes.
        why_lines: list[str] = []
        if gentle_correction:
            why_lines.append(gentle_correction)
        why_lines.append(AI_TRADE_CANDIDATES_CANONICAL_DEFINITION)
        if why:
            why_lines.append(f"Fit note: {why}")

        strat = str(top.get("strategy") or top.get("structure") or top.get("description") or "").strip() or None
        for ln in _strategy_explain(strat)[:3]:
            why_lines.append(ln)

        sections["B) Why"] = "\n".join([f"- {x}" for x in why_lines if x]).strip() or sections.get("B) Why", "")

        # C) Action: lead with context, then name it, then non-directive clause.
        action_lines = [
            f"- {title}",
            "- Based on current regime, futures alignment, and options positioning...",
            "- The highest-confidence option structure candidate right now is...",
            f"  - {line}",
            f"- {AI_TRADE_CANDIDATES_BOUNDARY_SENTENCE}",
        ]
        sections["C) Action"] = "\n".join(action_lines)

        # D) Risk + invalidation: deterministic invalidation + when not to take it.
        risk = sections.get("D) Risk + invalidation", "")
        extras = [
            "- This candidate becomes invalid if: futures bias flips, volatility regime shifts sharply, or confirmation breaks down.",
            "- When NOT to take it: avoid forcing trades, or if conditions degrade / data becomes stale.",
        ]
        for extra in extras:
            if extra not in risk:
                risk = (risk + "\n" + extra).strip() if risk else extra
        sections["D) Risk + invalidation"] = risk.strip()

    elif status == "NONE":
        why_lines = []
        if gentle_correction:
            why_lines.append(gentle_correction)
        why_lines.append(AI_TRADE_CANDIDATES_CANONICAL_DEFINITION)
        sections["B) Why"] = "\n".join([f"- {x}" for x in why_lines if x]).strip() or sections.get("B) Why", "")

        action_lines = [
            "- DO NOTHING.",
            f"- {AI_TRADE_CANDIDATES_NO_CANDIDATE_SENTENCE}",
        ]
        if why:
            action_lines.append(f"- Reason (brief): {why}")
        sections["C) Action"] = "\n".join(action_lines)
    elif status in {"STALE", "ERROR"}:
        why_lines = []
        if gentle_correction:
            why_lines.append(gentle_correction)
        why_lines.append(AI_TRADE_CANDIDATES_CANONICAL_DEFINITION)
        sections["B) Why"] = "\n".join([f"- {x}" for x in why_lines if x]).strip() or sections.get("B) Why", "")

        action_lines = [
            "- DO NOTHING.",
            "- Candidates are stale/unavailable, so I can’t surface an AI trade candidate right now.",
        ]
        if why:
            action_lines.append(f"- Reason (brief): {why}")
        sections["C) Action"] = "\n".join(action_lines)

    # Re-compose.
    rebuilt: list[str] = []
    for h in _ENVELOPE_HEADINGS:
        rebuilt.append(h)
        rebuilt.append((sections.get(h) or "(n/a)").strip())
        rebuilt.append("")

    return "\n".join(rebuilt).strip()


def tokens_from_tnt_state(*, symbol: str, tnt_state: Mapping[str, Any], confirm: str | None = None, confidence: float | None = None) -> CoachTokens:
    meta = tnt_state.get("meta") if isinstance(tnt_state.get("meta"), Mapping) else {}
    posture = tnt_state.get("posture") if isinstance(tnt_state.get("posture"), Mapping) else {}
    perms = tnt_state.get("permissions") if isinstance(tnt_state.get("permissions"), Mapping) else {}

    bias = str(posture.get("bias") or "NEUTRAL").upper()
    regime = str(posture.get("regime") or "UNKNOWN").upper()
    data_health = str(meta.get("data_health") or "UNKNOWN").upper()

    no_trade = bool(perms.get("no_trade"))
    eligibility = "NO_TRADE" if no_trade else "ELIGIBLE"

    df = meta.get("data_freshness_sec")
    data_freshness_sec = int(df) if isinstance(df, (int, float)) else None

    conf = confidence
    if conf is None:
        # Fallback mapping from conviction.
        conv = str(posture.get("conviction") or "LOW").upper()
        conf = 0.8 if conv == "HIGH" else (0.6 if conv in {"MED", "MEDIUM"} else 0.4)

    return CoachTokens(
        symbol=str(symbol or "SPY").upper(),
        regime=regime,
        bias=bias,
        confirm=str(confirm or "UNKNOWN").upper(),
        confidence=float(conf) if isinstance(conf, (int, float)) else None,
        eligibility=eligibility,
        data_health=data_health,
        data_freshness_sec=data_freshness_sec,
    )
