from datetime import datetime, timedelta, timezone

try:
    from zoneinfo import ZoneInfo

    _ET = ZoneInfo("America/New_York")
except Exception:  # pragma: no cover
    _ET = timezone.utc

from services.calendar.earnings_overlay import build_earnings_near_note, build_earnings_risk_overlay


def test_overlay_includes_expected_move_and_liquidity() -> None:
    ev = {
        "ts_utc": "2099-01-20T21:00:00Z",
        "session": "AMC",
        "confirmed": True,
        "expected_move_pct": 4.2,
        "liquidity_risk": "HIGH",
        "front_iv": 68,
    }
    o = build_earnings_risk_overlay(ev)
    assert "earnings" in o["headline"].lower()
    assert "expected move" in o["detail"]
    assert "liquidity: HIGH" in o["detail"]


def test_overlay_is_empty_without_timestamp() -> None:
    assert build_earnings_risk_overlay({}) == {}
    assert build_earnings_risk_overlay(None) == {}


def test_earnings_near_note_today_or_tomorrow_only() -> None:
    now_utc = datetime.now(timezone.utc)
    now_et = now_utc.astimezone(_ET)

    ts_et = (now_et + timedelta(days=1)).replace(hour=16, minute=30, second=0, microsecond=0)
    ts_utc = ts_et.astimezone(timezone.utc)
    ev_tomorrow = {
        "ts_utc": ts_utc.isoformat().replace("+00:00", "Z"),
        "session": "AMC",
        "confirmed": True,
        "expected_move_pct": 4.2,
        "liquidity_risk": "HIGH",
    }
    note = build_earnings_near_note(ev_tomorrow)
    assert note is not None
    assert "earnings" in note.lower()
    assert "tomorrow" in note.lower()
    assert " • " in note

    ev_future = {
        "ts_utc": "2099-01-20T21:00:00Z",
        "session": "AMC",
        "confirmed": True,
    }
    assert build_earnings_near_note(ev_future) is None
