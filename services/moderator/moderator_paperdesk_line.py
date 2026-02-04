from __future__ import annotations

from typing import Any

from services.futures.futures_context_line import MAX_AGE_S_DEFAULT, compute_context, render_confirmation_badge


def _coerce_conviction_label(confidence: Any) -> str:
    try:
        x = float(confidence)
    except Exception:
        return "LOW"
    if x >= 0.75:
        return "HIGH"
    if x >= 0.55:
        return "MED"
    return "LOW"


def render_paperdesk_moderator_line(
    *,
    direction: str,
    fut_scores: Any,
    tnt_confidence: Any = None,
    now_epoch: int | None = None,
    max_age_sec: int = MAX_AGE_S_DEFAULT,
) -> str:
    """Deterministic single-line moderator signal for Paper Desk posts.

    Always returns exactly one line (never None) so callers can append it
    without branching.

    Rules (deterministic):
    - If futures are fresh + strongly divergent: ⚠️ line with Δ/age.
    - Else if futures are fresh + aligned + confirm direction: ✅ line with Δ/age.
    - Else: 🧊 line (low conviction / transition / mixed / stale).
    """

    dir_up = (direction or "").strip().upper()
    if dir_up not in {"BULL", "BEAR"}:
        dir_up = "NEUTRAL"

    ctx = compute_context(fut_scores, now_epoch=now_epoch, max_age_sec=max_age_sec)

    div_txt = "n/a"
    if isinstance(ctx.divergence, (int, float)):
        div_txt = f"{float(ctx.divergence):.2f}"

    age_txt = "n/a"
    if isinstance(ctx.age_sec, int):
        age_txt = str(int(ctx.age_sec))

    badge = None
    try:
        badge = render_confirmation_badge(
            fut_scores,
            direction=dir_up,
            now_epoch=now_epoch,
            max_age_sec=max_age_sec,
        )
    except Exception:
        badge = None

    if badge == "⚠️ diverges":
        return (
            f"⚠️ Futures diverge (Δ={div_txt}, age={age_txt}s)"
            " — Bottom line: DO NOTHING | Watch: Δ<0.20 + reconfirm"
        )

    if badge and badge.startswith("✅ confirms") and dir_up in {"BULL", "BEAR"}:
        return (
            f"✅ Futures confirm {dir_up} (Δ={div_txt}, age={age_txt}s)"
            " — Bottom line: SELECTIVE | Watch: keep Δ<0.20"
        )

    conviction = _coerce_conviction_label(tnt_confidence)
    if ctx.state != "OK":
        return (
            f"🧊 Futures stale (Δ={div_txt}, age={age_txt}s)"
            f" — Bottom line: DO NOTHING | Watch: refresh (<{int(max_age_sec)}s)"
        )

    dir_label = dir_up if dir_up in {"BULL", "BEAR"} else "direction"
    if conviction == "LOW" or ctx.mode == "transition" or ctx.trend == "MIXED" or ctx.divergence_level in {"mixed", "unknown"}:
        return (
            f"🧊 Futures mixed/transition (Δ={div_txt}, age={age_txt}s)"
            f" — Bottom line: DO NOTHING | Watch: confirm {dir_label} + Δ<0.20"
        )

    # Fresh + trendy, but no confirm badge (e.g., direction mismatch).
    return (
        f"🧊 Futures not confirming (Δ={div_txt}, age={age_txt}s)"
        f" — Bottom line: DO NOTHING | Watch: confirm {dir_label}"
    )
