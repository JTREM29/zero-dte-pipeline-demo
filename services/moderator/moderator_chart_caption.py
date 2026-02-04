from __future__ import annotations

from typing import Any

from services.futures.futures_context_line import MAX_AGE_S_DEFAULT, compute_context


_PREFIX = "🧭 Moderator: "


def render_chart_moderator_line(
    fut_scores: Any,
    *,
    now_epoch: int | None = None,
    max_age_sec: int = MAX_AGE_S_DEFAULT,
) -> str:
    """Deterministic moderator line for chart captions.

    Directionless by design: charts cover multiple views; we only reflect
    futures alignment/divergence + freshness.
    """

    ctx = compute_context(fut_scores, now_epoch=now_epoch, max_age_sec=max_age_sec)

    div_txt = "n/a"
    if isinstance(ctx.divergence, (int, float)):
        div_txt = f"{float(ctx.divergence):.2f}"

    age_txt = "n/a"
    if isinstance(ctx.age_sec, int):
        age_txt = str(int(ctx.age_sec))

    if ctx.state != "OK":
        return _PREFIX + f"🧊 Futures stale (age={age_txt}s)"

    if ctx.divergence_level == "divergent":
        return _PREFIX + f"⚠️ Futures diverge (Δ={div_txt}, age={age_txt}s)"

    if ctx.divergence_level == "aligned" and ctx.trend in {"RISK-ON", "RISK-OFF"}:
        return _PREFIX + f"✅ Futures aligned ({ctx.trend}) (Δ={div_txt}, age={age_txt}s)"

    # mixed/unknown
    return _PREFIX + f"🧊 Futures mixed/transition (Δ={div_txt}, age={age_txt}s)"


def append_chart_moderator_line(
    caption: str | None,
    fut_scores: Any,
    *,
    now_epoch: int | None = None,
    max_age_sec: int = MAX_AGE_S_DEFAULT,
) -> str:
    base = str(caption or "").rstrip()
    line = render_chart_moderator_line(fut_scores, now_epoch=now_epoch, max_age_sec=max_age_sec)

    # Idempotent: avoid double-bannering.
    if _PREFIX in base or line in base:
        return base

    if not base.strip():
        return line
    return base + "\n" + line
