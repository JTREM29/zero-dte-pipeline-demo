from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from services.futures.futures_context_line import MAX_AGE_S_DEFAULT, compute_context


@dataclass(frozen=True)
class UncertaintyDecision:
    level: str  # NORMAL | ELEVATED
    key: str
    message: str | None


def evaluate_uncertainty(
    fut_scores: Any,
    *,
    now_epoch: int | None = None,
    max_age_sec: int = MAX_AGE_S_DEFAULT,
) -> UncertaintyDecision:
    """Pure evaluator for elevated-uncertainty state.

    Elevated uncertainty is intended to catch conditions where automation should
    be extra cautious: stale futures, strong divergence, or transition/mixed tape.
    """

    ctx = compute_context(fut_scores, now_epoch=now_epoch, max_age_sec=max_age_sec)

    div_txt = "n/a"
    if isinstance(ctx.divergence, (int, float)):
        div_txt = f"{float(ctx.divergence):.2f}"

    age_txt = "n/a"
    if isinstance(ctx.age_sec, int):
        age_txt = str(int(ctx.age_sec))

    reason = None
    elevated = False

    if ctx.state != "OK":
        elevated = True
        reason = "stale"
    elif ctx.divergence_level == "divergent":
        elevated = True
        reason = "divergent"
    elif ctx.mode == "transition" and ctx.divergence_level != "aligned":
        elevated = True
        reason = "transition"
    elif ctx.trend == "MIXED" and ctx.divergence_level in {"mixed", "unknown"}:
        elevated = True
        reason = "mixed"

    if not elevated:
        return UncertaintyDecision(level="NORMAL", key="normal", message=None)

    trend = str(ctx.trend or "MIXED")
    mode = str(ctx.mode or "transition")
    div_level = str(ctx.divergence_level or "unknown")
    key = f"elevated:{reason}:{ctx.state}:{trend}:{mode}:{div_level}"
    msg = f"🛟 Canary: elevated uncertainty ({reason}) — Futures {trend}/{mode} Δ={div_txt} age={age_txt}s"
    return UncertaintyDecision(level="ELEVATED", key=key, message=msg)


class UncertaintyCanary:
    """State-change-only, rate-limited canary poster."""

    def __init__(self, *, min_post_sec: int = 900) -> None:
        self._min_post_sec = max(30, int(min_post_sec))
        self._last_post_epoch: int | None = None
        self._last_post_key: str | None = None
        self._last_seen_level: str = "NORMAL"
        self._last_alerted_level: str = "NORMAL"

    def maybe_message(
        self,
        fut_scores: Any,
        *,
        now_epoch: int,
        max_age_sec: int = MAX_AGE_S_DEFAULT,
    ) -> str | None:
        d = evaluate_uncertainty(fut_scores, now_epoch=now_epoch, max_age_sec=max_age_sec)
        self._last_seen_level = str(d.level)

        if d.level != "ELEVATED":
            # Reset so a future re-entry can alert.
            self._last_alerted_level = "NORMAL"
            self._last_post_key = None
            return None

        # Only post when entering ELEVATED (state-change-only).
        if not d.message or self._last_alerted_level == "ELEVATED":
            return None

        if self._last_post_key == d.key:
            return None

        if self._last_post_epoch is not None:
            if int(now_epoch) - int(self._last_post_epoch) < int(self._min_post_sec):
                return None

        self._last_post_epoch = int(now_epoch)
        self._last_post_key = str(d.key)
        self._last_alerted_level = "ELEVATED"
        return str(d.message)
