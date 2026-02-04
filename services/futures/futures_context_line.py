from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

from .futures_models import FutScores


@dataclass(frozen=True)
class FuturesContext:
    state: str  # OK | STALE | MISSING
    age_sec: int | None
    updated_utc: int | None
    trend: str  # RISK-ON | RISK-OFF | MIXED
    mode: str  # trend | chop | transition
    es_trend: float | None
    nq_impulse: float | None
    vol_mult: float | None
    breadth_bearish: int | None
    breadth_total: int | None
    divergence: float | None
    divergence_level: str  # aligned | mixed | divergent | unknown


MAX_AGE_S_DEFAULT = 120
WARN_AGE_S_DEFAULT = 300

DIVERGENCE_WEAK = 0.20
DIVERGENCE_STRONG = 0.35


def _now_epoch(now_epoch: int | None = None) -> int:
    if now_epoch is None:
        return int(time.time())
    return int(now_epoch)


def coerce_scores(scores: Any) -> FutScores | None:
    if scores is None:
        return None
    if isinstance(scores, FutScores):
        return scores
    if isinstance(scores, dict):
        try:
            trend = scores.get("trend") if isinstance(scores.get("trend"), dict) else {}
            impulse = scores.get("impulse") if isinstance(scores.get("impulse"), dict) else {}
            return FutScores(
                trend={str(k): float(v) for k, v in (trend or {}).items() if v is not None},
                impulse={str(k): float(v) for k, v in (impulse or {}).items() if v is not None},
                vol_mult=float(scores.get("vol_mult", 1.0) or 1.0),
                breadth_bearish=int(scores.get("breadth_bearish", 0) or 0),
                breadth_total=int(scores.get("breadth_total", 3) or 3),
                regime=str(scores.get("regime", "NEUTRAL") or "NEUTRAL"),
                updated_utc=int(scores.get("updated_utc", 0) or 0),
            )
        except Exception:
            return None
    return None


def scores_age_sec(scores: Any, *, now_epoch: int | None = None) -> int | None:
    s = coerce_scores(scores)
    if s is None:
        return None
    try:
        ts = int(s.updated_utc or 0)
    except Exception:
        ts = 0
    if ts <= 0:
        return None
    return max(0, _now_epoch(now_epoch) - ts)


def is_fresh(scores: Any, *, max_age_sec: int = 120, now_epoch: int | None = None) -> bool:
    age = scores_age_sec(scores, now_epoch=now_epoch)
    if age is None:
        return False
    return int(age) <= int(max(5, int(max_age_sec)))


def _risk_label(regime: str) -> str:
    r = (regime or "").strip().upper()
    if "RISK_OFF" in r:
        return "RISK-OFF"
    if "RISK_ON" in r:
        return "RISK-ON"
    if "CHOP" in r:
        return "MIXED"
    return "MIXED"


def _mode_label(regime: str) -> str:
    r = (regime or "").strip().upper()
    if "CHOP" in r:
        return "chop"
    if "RISK_OFF" in r or "RISK_ON" in r:
        return "trend"
    return "transition"


def _divergence_level(div: float | None) -> str:
    if div is None:
        return "unknown"
    if float(div) < float(DIVERGENCE_WEAK):
        return "aligned"
    if float(div) < float(DIVERGENCE_STRONG):
        return "mixed"
    return "divergent"


def compute_context(scores: Any, *, now_epoch: int | None = None, max_age_sec: int = 120) -> FuturesContext:
    s = coerce_scores(scores)
    if s is None:
        return FuturesContext(
            state="MISSING",
            age_sec=None,
            updated_utc=None,
            trend="MIXED",
            mode="transition",
            es_trend=None,
            nq_impulse=None,
            vol_mult=None,
            breadth_bearish=None,
            breadth_total=None,
            divergence=None,
            divergence_level="unknown",
        )

    age = scores_age_sec(s, now_epoch=now_epoch)
    state = "OK" if is_fresh(s, max_age_sec=max_age_sec, now_epoch=now_epoch) else "STALE"

    es = None
    nq = None
    try:
        es = s.trend.get("ES")
    except Exception:
        es = None
    try:
        nq = s.impulse.get("NQ")
    except Exception:
        nq = None

    div = None
    try:
        if es is not None and nq is not None:
            div = abs(float(es) - float(nq))
    except Exception:
        div = None

    trend = _risk_label(s.regime)
    mode = _mode_label(s.regime)

    return FuturesContext(
        state=state,
        age_sec=age,
        updated_utc=int(s.updated_utc) if isinstance(s.updated_utc, int) else None,
        trend=trend,
        mode=mode,
        es_trend=float(es) if isinstance(es, (int, float)) else None,
        nq_impulse=float(nq) if isinstance(nq, (int, float)) else None,
        vol_mult=float(s.vol_mult) if isinstance(s.vol_mult, (int, float)) else None,
        breadth_bearish=int(s.breadth_bearish) if isinstance(s.breadth_bearish, int) else None,
        breadth_total=int(s.breadth_total) if isinstance(s.breadth_total, int) else None,
        divergence=float(div) if isinstance(div, (int, float)) else None,
        divergence_level=_divergence_level(div if isinstance(div, (int, float)) else None),
    )


def render_futures_context_line(
    scores: Any,
    *,
    now_epoch: int | None = None,
    max_age_sec: int = MAX_AGE_S_DEFAULT,
    warn_age_sec: int | None = None,
) -> str | None:
    ctx = compute_context(scores, now_epoch=now_epoch, max_age_sec=max_age_sec)
    if ctx.state == "MISSING":
        return None

    # Stale-quiet by default.
    if ctx.state != "OK":
        if warn_age_sec is None or ctx.age_sec is None:
            return None
        if int(ctx.age_sec) > int(warn_age_sec):
            return None

    age = int(ctx.age_sec or 0)
    div_txt = "n/a"
    if isinstance(ctx.divergence, (int, float)):
        div_txt = f"{float(ctx.divergence):.2f}"

    line = f"📊 Futures: {ctx.trend} | {ctx.mode} | Δ={div_txt} | age={age}s"
    if ctx.state != "OK" and warn_age_sec is not None:
        line = line + " (aging)"
    return line


def render_confirmation_badge(
    scores: Any,
    *,
    direction: str,
    now_epoch: int | None = None,
    max_age_sec: int = MAX_AGE_S_DEFAULT,
) -> str | None:
    ctx = compute_context(scores, now_epoch=now_epoch, max_age_sec=max_age_sec)
    if ctx.state != "OK":
        return None

    dir_up = (direction or "").strip().upper()
    if dir_up not in {"BULL", "BEAR"}:
        return None

    # Strong divergence gets a warning regardless of direction.
    if ctx.divergence_level == "divergent":
        return "⚠️ diverges"

    # Only confirm when aligned + clearly aligned (weak/mixed => silent).
    if ctx.divergence_level != "aligned":
        return None

    if ctx.trend == "RISK-ON" and dir_up == "BULL":
        return "✅ confirms BULL"
    if ctx.trend == "RISK-OFF" and dir_up == "BEAR":
        return "✅ confirms BEAR"
    return None
