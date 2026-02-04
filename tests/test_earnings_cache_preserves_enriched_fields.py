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


def test_set_earnings_preserves_enriched_fields_from_existing_blob():
    r = FakeRedis()
    cal = CalendarService(r)

    existing = {
        "ts_utc": "2026-01-10T21:10:00+00:00",
        "confirmed": True,
        "source": "benzinga",
        "session": "AMC",
        "blackout": {"pre_min": 45, "post_min": 30},
        "refreshed_utc": "2026-01-10T21:11:00+00:00",
        # Premium/enriched fields that MUST NOT be wiped by periodic refresh.
        "expected_move_pct": 0.043,
        "expected_move": {"pct": 0.043, "price": 250.0, "dollars": 10.8},
        "expected_move_refreshed_utc": "2026-01-10T21:12:00+00:00",
        "expected_move_source": "polygon_weekly_atm",
        "liquidity_risk": "HIGH IMPACT",
        "iv_state": "MED",
        "history": [{"ts_utc": "2025-11-04T21:00:00+00:00", "move_pct": 0.025, "gap_pct": -0.027, "tag": "fade"}],
        "_reactions_cache": {"last4": ["Nov 04: 2.5% (gap -2.7%; gap, fade)"]},
    }
    r.set("cal:earnings:SPY", json.dumps(existing))

    new_ts = datetime(2026, 2, 18, 21, 10, tzinfo=timezone.utc)
    cal.set_earnings(symbol="SPY", ts_utc=new_ts, confirmed=False, source="benzinga")

    raw = r.get("cal:earnings:SPY")
    assert raw is not None
    obj = json.loads(raw)

    # Canonical fields updated.
    assert obj["ts_utc"].startswith("2026-02-18")
    assert obj["confirmed"] is False
    assert obj["source"] == "benzinga"

    # Enriched fields preserved.
    assert obj.get("expected_move_pct") == 0.043
    assert obj.get("expected_move", {}).get("pct") == 0.043
    assert obj.get("expected_move_refreshed_utc") == "2026-01-10T21:12:00+00:00"
    assert obj.get("expected_move_source") == "polygon_weekly_atm"
    assert obj.get("liquidity_risk") == "HIGH IMPACT"
    iv = obj.get("iv_state")
    assert isinstance(iv, dict)
    assert iv.get("crush_risk") == "MED"
    assert isinstance(obj.get("history"), list)

    # Freshness keys should be present.
    per_sym = r.get("cal:earnings:refreshed_utc:SPY")
    assert per_sym is not None
    assert int(str(per_sym)) > 0
