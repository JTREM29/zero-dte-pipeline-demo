from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
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


def _month_start(d: date) -> date:
    return date(d.year, d.month, 1)


def _month_add(d: date, months: int) -> date:
    y = d.year + (d.month - 1 + months) // 12
    m = (d.month - 1 + months) % 12 + 1
    return date(y, m, 1)


def _first_weekday_of_month(y: int, m: int, weekday: int) -> date:
    """weekday: Monday=0 ... Sunday=6"""

    d = date(y, m, 1)
    shift = (weekday - d.weekday()) % 7
    return d + timedelta(days=shift)


def _nth_weekday_of_month(y: int, m: int, weekday: int, n: int) -> date:
    if n <= 0:
        raise ValueError("n must be >= 1")
    first = _first_weekday_of_month(y, m, weekday)
    return first + timedelta(days=7 * (n - 1))


def _last_weekday_of_month(y: int, m: int, weekday: int) -> date:
    next_month = _month_add(date(y, m, 1), 1)
    last = next_month - timedelta(days=1)
    shift = (last.weekday() - weekday) % 7
    return last - timedelta(days=shift)


def _nth_business_day_of_month(y: int, m: int, n: int) -> date:
    if n <= 0:
        raise ValueError("n must be >= 1")
    d = date(y, m, 1)
    count = 0
    while True:
        if d.weekday() < 5:
            count += 1
            if count == n:
                return d
        d = d + timedelta(days=1)


def _macro_schedule_path() -> Path:
    raw = (os.getenv("TNT_MACRO_SCHEDULE_PATH") or "").strip()
    if raw:
        return Path(raw)
    # repo_root/services/macro_calendar.py -> repo_root
    return Path(__file__).resolve().parents[1] / "data" / "macro_schedule.json"


def macro_schedule_path_str() -> str:
    """Return the macro schedule path used for overrides (for embeds/diagnostics)."""

    try:
        return str(_macro_schedule_path())
    except Exception:
        return "data/macro_schedule.json"


def _parse_dt_et(s: object) -> datetime | None:
    try:
        ss = str(s or "").strip()
        if not ss:
            return None
        # Support trailing 'Z'.
        if ss.endswith("Z"):
            ss = ss[:-1] + "+00:00"
        dt = datetime.fromisoformat(ss)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=ET)
        return dt.astimezone(ET)
    except Exception:
        return None


_TZ_SUFFIX_RE = re.compile(r"(Z|[+-]\d\d:\d\d)$")


def _dt_string_has_tz(s: str) -> bool:
    try:
        return bool(_TZ_SUFFIX_RE.search(str(s or "").strip()))
    except Exception:
        return False


def _dt_string_is_et_offset(s: str) -> bool:
    try:
        ss = str(s or "").strip()
        return ss.endswith("-05:00") or ss.endswith("-04:00")
    except Exception:
        return False


def _load_schedule_records() -> list[dict[str, Any]]:
    """Load raw schedule JSON records (for validation and error messages)."""

    path = _macro_schedule_path()
    try:
        if not path.exists() or not path.is_file():
            return []
        raw = path.read_text(encoding="utf-8", errors="ignore")
        payload = json.loads(raw)
    except Exception:
        return []
    return payload if isinstance(payload, list) else []


def _load_schedule_events() -> list[MacroEvent]:
    """Load exact release timestamps from a JSON schedule file.

    Format: list[dict] with keys:
      - id (required)
      - title (required)
      - dt_et (required, ISO8601 string)
      - impact/importance (optional: HIGH/MED)
      - blackout_before_min / blackout_after_min (optional)
      - tags (optional: list[str])

    Schedule entries override any code-generated event with the same id.
    """

    payload = _load_schedule_records()
    if not payload:
        return []

    out: list[MacroEvent] = []
    for item in payload:
        if not isinstance(item, dict):
            continue
        eid = str(item.get("id") or "").strip()
        title = str(item.get("title") or "").strip()
        dt = _parse_dt_et(item.get("dt_et"))
        if not eid or not title or dt is None:
            continue

        impact = str(item.get("impact") or item.get("importance") or "").strip().upper() or "MED"
        if impact not in {"HIGH", "MED"}:
            impact = "MED"

        b_before = item.get("blackout_before_min")
        b_after = item.get("blackout_after_min")
        try:
            bb = int(b_before) if b_before is not None else 45
        except Exception:
            bb = 45
        try:
            ba = int(b_after) if b_after is not None else 30
        except Exception:
            ba = 30

        tags_raw = item.get("tags")
        tags: tuple[str, ...] = ()
        if isinstance(tags_raw, (list, tuple)):
            tags = tuple(str(x).strip() for x in tags_raw if str(x).strip())

        out.append(
            MacroEvent(
                id=eid,
                title=title,
                dt_et=dt,
                importance=impact,
                blackout_before_min=bb,
                blackout_after_min=ba,
                tags=tags,
            )
        )
    return out


def _monthly_recurring_events(start_et: datetime, end_et: datetime) -> list[MacroEvent]:
    """Generate a small set of monthly recurring macro releases.

    This is a pragmatic Phase-1 compromise: we want broad coverage without
    needing a dedicated calendar API.
    """

    start_m = _month_start(start_et.date())
    end_m = _month_start(end_et.date())
    months = (end_m.year - start_m.year) * 12 + (end_m.month - start_m.month)

    out: list[MacroEvent] = []
    for i in range(months + 1):
        m0 = _month_add(start_m, i)
        y, m = m0.year, m0.month

        # Jobs Report (NFP + Unemployment Rate): first Friday @ 08:30 ET (HIGH).
        jobs = _nth_weekday_of_month(y, m, weekday=4, n=1)  # Friday
        out.append(
            MacroEvent(
                id=f"NFP_{y:04d}-{m:02d}",
                title="Jobs Report (NFP + Unemployment Rate)",
                dt_et=_dt_et(y, m, jobs.day, 8, 30),
                importance="HIGH",
                tags=("labor", "nfp", "unemployment"),
            )
        )

        # PPI: commonly early/mid-month @ 08:30 ET (MED). Approximate: 2nd Thursday.
        ppi = _nth_weekday_of_month(y, m, weekday=3, n=2)  # Thursday
        out.append(
            MacroEvent(
                id=f"PPI_{y:04d}-{m:02d}",
                title="PPI",
                dt_et=_dt_et(y, m, ppi.day, 8, 30),
                importance="MED",
                tags=("inflation", "ppi"),
            )
        )

        # ISM PMI (Manufacturing): typically 1st business day @ 10:00 ET (MED).
        ism_mfg = _nth_business_day_of_month(y, m, n=1)
        out.append(
            MacroEvent(
                id=f"ISM_MFG_PMI_{y:04d}-{m:02d}",
                title="ISM PMI (Manufacturing)",
                dt_et=_dt_et(y, m, ism_mfg.day, 10, 0),
                importance="MED",
                tags=("pmi", "ism", "manufacturing"),
            )
        )

        # ISM PMI (Services): typically 3rd business day @ 10:00 ET (MED).
        ism_srv = _nth_business_day_of_month(y, m, n=3)
        out.append(
            MacroEvent(
                id=f"ISM_SERVICES_PMI_{y:04d}-{m:02d}",
                title="ISM PMI (Services)",
                dt_et=_dt_et(y, m, ism_srv.day, 10, 0),
                importance="MED",
                tags=("pmi", "ism", "services"),
            )
        )

        # Consumer Confidence (Conference Board): last Tuesday @ 10:00 ET (MED).
        cc = _last_weekday_of_month(y, m, weekday=1)  # Tuesday
        out.append(
            MacroEvent(
                id=f"CONSUMER_CONFIDENCE_{y:04d}-{m:02d}",
                title="Consumer Confidence",
                dt_et=_dt_et(y, m, cc.day, 10, 0),
                importance="MED",
                tags=("consumer",),
            )
        )

    return out


def _rule_events(now_et: datetime, end_et: datetime) -> list[MacroEvent]:
    """Rule-based + hardcoded events (without schedule overrides)."""

    events: list[MacroEvent] = [
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

    try:
        events.extend(_monthly_recurring_events(now_et, end_et))
    except Exception:
        pass

    return events


def _merged_events_with_sources(now_et: datetime, end_et: datetime) -> tuple[list[MacroEvent], dict[str, str]]:
    """Merge schedule overrides into rule-based events and track provenance."""

    rule = _rule_events(now_et, end_et)
    scheduled = _load_schedule_events()
    merged: dict[str, MacroEvent] = {e.id: e for e in rule}
    src: dict[str, str] = {e.id: "rule" for e in rule}
    for s in scheduled:
        merged[s.id] = s
        src[s.id] = "schedule"
    return list(merged.values()), src


def get_next_macro_events(now_et: datetime, horizon_days: int = 14) -> List[MacroEvent]:
    """Phase 1: hardcoded schedule table.

    This is intentionally simple and editable without touching other logic.
    """

    end = now_et + timedelta(days=int(horizon_days))
    try:
        events, _src = _merged_events_with_sources(now_et, end)
    except Exception:
        events = _rule_events(now_et, end)

    out = [e for e in events if now_et <= e.dt_et <= end]
    out.sort(key=lambda e: e.dt_et)
    return out


def validate_macro_schedule(now_et: datetime | None = None, *, horizon_days: int = 45) -> dict[str, Any]:
    """Validate macro schedule overrides and posting readiness.

    Returns a dict with:
      - ok: bool
      - errors: list[str]
      - warnings: list[str]
      - counts: dict
      - schedule_path: str
    """

    now = now_et or datetime.now(tz=ET)
    end = now + timedelta(days=int(horizon_days))

    schedule_path = macro_schedule_path_str()
    errors: list[str] = []
    warnings: list[str] = []

    # Posting readiness: channel id.
    raw_ch = (os.getenv("TNT_CALENDAR_EARNINGS_CHANNEL_ID") or "").strip()
    if not raw_ch:
        warnings.append("TNT_CALENDAR_EARNINGS_CHANNEL_ID is not set (autopost will be silent).")
    else:
        try:
            if int(raw_ch) <= 0:
                warnings.append("TNT_CALENDAR_EARNINGS_CHANNEL_ID is not a positive int.")
        except Exception:
            warnings.append("TNT_CALENDAR_EARNINGS_CHANNEL_ID is not an int.")

    # Load raw records for timezone / parse checks.
    raw_records = _load_schedule_records()
    schedule_events = _load_schedule_events()
    schedule_ids = {e.id for e in schedule_events}

    # Schedule record sanity.
    for i, item in enumerate(raw_records):
        if not isinstance(item, dict):
            warnings.append(f"schedule[{i}] is not an object")
            continue
        eid = str(item.get("id") or "").strip() or f"(missing id @ {i})"
        dt_s = str(item.get("dt_et") or "").strip()
        if not dt_s:
            errors.append(f"{eid}: missing dt_et")
            continue

        if not _dt_string_has_tz(dt_s):
            warnings.append(f"{eid}: dt_et has no timezone/offset suffix (recommend -05:00/-04:00)")
        if _dt_string_has_tz(dt_s) and not _dt_string_is_et_offset(dt_s):
            warnings.append(f"{eid}: dt_et is not an ET offset string (-05:00/-04:00)")

        dt = _parse_dt_et(dt_s)
        if dt is None:
            errors.append(f"{eid}: dt_et is not parseable: {dt_s}")
            continue

    # Build merged horizon events and provenance.
    try:
        merged, src = _merged_events_with_sources(now, end)
    except Exception:
        merged = _rule_events(now, end)
        src = {e.id: "rule" for e in merged}

    upcoming = [e for e in merged if now <= e.dt_et <= end]
    upcoming.sort(key=lambda e: e.dt_et)

    # HIGH impact must be exact: warn if HIGH came from rule.
    for e in upcoming:
        if str(e.importance).upper() == "HIGH" and src.get(e.id) != "schedule":
            warnings.append(f"{e.id}: HIGH impact event not overridden by schedule (source=rule)")

    # Required ID coverage (warn-only): CPI/NFP monthly, plus any in-horizon FOMC/GDP ids.
    months: set[str] = set()
    cur = date(now.year, now.month, 1)
    last = date(end.year, end.month, 1)
    mcount = (last.year - cur.year) * 12 + (last.month - cur.month)
    for k in range(mcount + 1):
        d0 = _month_add(cur, k)
        months.add(f"{d0.year:04d}-{d0.month:02d}")

    for ym in sorted(months):
        if f"CPI_{ym}" not in schedule_ids:
            warnings.append(f"Missing exact override in schedule: CPI_{ym}")
        if f"NFP_{ym}" not in schedule_ids:
            warnings.append(f"Missing exact override in schedule: NFP_{ym}")
        if f"PPI_{ym}" not in schedule_ids:
            warnings.append(f"Missing exact override in schedule: PPI_{ym}")

    for e in upcoming:
        if e.id.startswith("FOMC_DECISION_") and e.id not in schedule_ids:
            warnings.append(f"Missing exact override in schedule: {e.id}")
        if e.id.startswith("GDP_ADV_") and e.id not in schedule_ids:
            warnings.append(f"Missing exact override in schedule: {e.id}")

    # Duplicate timestamp collisions.
    by_ts: dict[str, list[str]] = {}
    for e in upcoming:
        key = e.dt_et.replace(second=0, microsecond=0).isoformat()
        by_ts.setdefault(key, []).append(e.id)
    for ts, ids in by_ts.items():
        if len(ids) >= 2:
            warnings.append(f"Timestamp collision {ts}: {sorted(ids)}")

    ok = not errors
    return {
        "ok": ok,
        "schedule_path": schedule_path,
        "horizon_days": int(horizon_days),
        "checked_range_et": {"start": now.isoformat(), "end": end.isoformat()},
        "counts": {
            "schedule_records": int(len(raw_records)),
            "schedule_events_loaded": int(len(schedule_events)),
            "upcoming_events": int(len(upcoming)),
            "warnings": int(len(warnings)),
            "errors": int(len(errors)),
        },
        "warnings": warnings,
        "errors": errors,
    }


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
