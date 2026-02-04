from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Dict, List

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover
    from backports.zoneinfo import ZoneInfo  # type: ignore


ET = ZoneInfo("America/New_York")


@dataclass(frozen=True)
class MacroEvent:
    id: str
    title: str
    dt_et: datetime
    importance: str  # HIGH / MED
    blackout_before_min: int = 45
    blackout_after_min: int = 30
    tags: tuple[str, ...] = ()


def _dt_et(y: int, m: int, d: int, hh: int, mm: int) -> datetime:
    return datetime(y, m, d, hh, mm, tzinfo=ET)


def get_next_macro_events(now_et: datetime, horizon_days: int = 14) -> List[MacroEvent]:
    """Phase 1: hardcoded schedule table.

    This is intentionally simple and editable without touching other logic.
    """

    end = now_et + timedelta(days=int(horizon_days))

    # TODO: Update these dates for the real release schedule.
    events = [
        MacroEvent(
            id="CPI_2026-02",
            title="CPI",
            dt_et=_dt_et(2026, 2, 11, 8, 30),
            importance="HIGH",
            tags=("inflation",),
        ),
        MacroEvent(
            id="FOMC_DECISION_2026-03",
            title="FOMC Decision",
            dt_et=_dt_et(2026, 3, 18, 14, 0),
            importance="HIGH",
            tags=("rates",),
        ),
        MacroEvent(
            id="FOMC_PRESSER_2026-03",
            title="Powell Presser",
            dt_et=_dt_et(2026, 3, 18, 14, 30),
            importance="HIGH",
            tags=("rates",),
        ),
        MacroEvent(
            id="GDP_ADV_2026-Q1",
            title="GDP (Advance)",
            dt_et=_dt_et(2026, 4, 30, 8, 30),
            importance="MED",
            tags=("growth",),
        ),
    ]

    out = [e for e in events if now_et <= e.dt_et <= end]
    out.sort(key=lambda e: e.dt_et)
    return out


def macro_blackout_state(now_et: datetime, horizon_days: int = 14) -> Dict[str, Any]:
    """Returns blackout info if inside any blackout window."""

    events = get_next_macro_events(now_et - timedelta(days=1), horizon_days=int(horizon_days) + 1)
    for e in events:
        start = e.dt_et - timedelta(minutes=int(e.blackout_before_min))
        end = e.dt_et + timedelta(minutes=int(e.blackout_after_min))
        if start <= now_et <= end:
            return {
                "active": True,
                "event_id": e.id,
                "title": e.title,
                "importance": e.importance,
                "blackout_ends_et": end.isoformat(),
            }
    return {"active": False}


def macro_trade_gate(now_et: datetime | None = None, *, importance: str = "HIGH") -> tuple[bool, Dict[str, Any]]:
    """Convenience helper for trade eligibility.

    Returns (trade_allowed, state_dict).
    """

    now = now_et or datetime.now(tz=ET)
    state = macro_blackout_state(now)
    if bool(state.get("active")) and str(state.get("importance") or "").upper() == str(importance).upper():
        return False, state
    return True, state
