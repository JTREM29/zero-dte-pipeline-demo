from __future__ import annotations

from datetime import date, datetime, timezone

from services.triple_expiry.resolver import resolve_mwf_expiry


def test_resolver_blocks_non_mwf() -> None:
    # 2026-01-20 is Tuesday
    d = date(2026, 1, 20)
    r = resolve_mwf_expiry(symbol="NVDA", today_et=d)
    assert r.ok is False
    assert r.reason == "not_mwf"


def test_resolver_blocks_non_universe() -> None:
    d = date(2026, 1, 21)  # Wednesday
    r = resolve_mwf_expiry(symbol="SPY", today_et=d)
    assert r.ok is False
    assert r.reason == "not_in_universe"


def test_resolver_blocks_earnings_day() -> None:
    d = date(2026, 1, 21)  # Wednesday
    # Earnings at 21:10 UTC is 16:10 ET (same date)
    earnings = {"ts_utc": datetime(2026, 1, 21, 21, 10, tzinfo=timezone.utc).isoformat(), "confirmed": True}
    r = resolve_mwf_expiry(symbol="NVDA", today_et=d, earnings_payload=earnings)
    assert r.ok is False
    assert r.reason == "earnings_blocked"


def test_resolver_ok_on_mwf() -> None:
    d = date(2026, 1, 23)  # Friday
    r = resolve_mwf_expiry(symbol="NVDA", today_et=d, available_expiries=["2026-01-23"])
    assert r.ok is True
    assert r.reason == "ok"
    assert r.expiry_ymd == "2026-01-23"


def test_resolver_no_expiry_available_on_mwf_when_missing() -> None:
    d = date(2026, 1, 23)  # Friday
    r = resolve_mwf_expiry(symbol="NVDA", today_et=d, available_expiries=["2026-01-21", "2026-01-26"])
    assert r.ok is False
    assert r.reason == "no_expiry_available"
