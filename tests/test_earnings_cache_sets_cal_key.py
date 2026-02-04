from __future__ import annotations

import json
from datetime import datetime, timezone

from services.calendar.calendar_service import CalendarService


class FakeRedis:
    def __init__(self):
        self.kv = {}
        self.expires = {}

    def set(self, key, value):
        self.kv[key] = value
        return True

    def get(self, key):
        return self.kv.get(key)

    def expire(self, key, ttl):
        self.expires[key] = int(ttl)
        return True


def test_earnings_cache_sets_cal_key():
    r = FakeRedis()
    cal = CalendarService(r)

    ts = datetime(2026, 1, 18, 21, 10, tzinfo=timezone.utc)
    cal.set_earnings(symbol="SPY", ts_utc=ts, confirmed=False, source="benzinga", ttl_sec=14 * 24 * 3600)

    raw = r.get("cal:earnings:SPY")
    assert raw is not None
    obj = json.loads(raw)
    assert obj["ts_utc"].startswith("2026-01-18")
    assert obj["confirmed"] is False
    assert obj["source"] == "benzinga"

    assert r.expires.get("cal:earnings:SPY") == 14 * 24 * 3600

    # Premium freshness keys (epoch seconds).
    per_sym = r.get("cal:earnings:refreshed_utc:SPY")
    assert per_sym is not None
    assert int(str(per_sym)) > 0

    global_ts = r.get("cal:earnings:last_refresh_utc")
    assert global_ts is not None
    assert int(str(global_ts)) > 0
