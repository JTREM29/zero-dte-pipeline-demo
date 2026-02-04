from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

from . import GateResult


def _parse_iso(ts: str) -> datetime | None:
    if not ts:
        return None
    try:
        raw = ts.strip().replace("Z", "+00:00")
        dt = datetime.fromisoformat(raw)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        return None


def macro_blackout(
    calendar: Any,
    *,
    now_utc: datetime,
    event_types: Iterable[str] | None,
    pre_minutes: int,
    post_minutes: int,
) -> GateResult:
    """Skip if currently inside a macro event blackout window.

    Uses Redis zset `cal:macro:upcoming` via CalendarService.macro_events_between.
    """

    try:
        pre = int(pre_minutes)
        post = int(post_minutes)
    except Exception:
        pre = 0
        post = 0

    types = {str(t).strip().upper() for t in (event_types or []) if str(t).strip()}

    # Fetch only within a bounded window for speed.
    start = now_utc - timedelta(hours=24)
    end = now_utc + timedelta(hours=24)
    try:
        events = calendar.macro_events_between(start_utc=start, end_utc=end)
    except Exception as exc:
        return GateResult(ok=True, details={"warn": f"macro_fetch_failed:{type(exc).__name__}"})

    for ev in events or []:
        if not isinstance(ev, dict):
            continue
        typ = str(ev.get("type") or "").strip().upper()
        if types and typ not in types:
            continue
        ts = _parse_iso(str(ev.get("ts_utc") or ""))
        if ts is None:
            continue
        w0 = ts - timedelta(minutes=pre)
        w1 = ts + timedelta(minutes=post)
        if w0 <= now_utc <= w1:
            return GateResult(
                ok=False,
                code="SKIP_MACRO_WINDOW",
                note=f"macro {typ or '?'} window",
                details={"event_type": typ, "event_ts_utc": ts.isoformat(), "pre_min": pre, "post_min": post},
            )

    return GateResult(ok=True)
