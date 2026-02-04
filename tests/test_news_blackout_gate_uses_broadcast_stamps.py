from __future__ import annotations

from datetime import datetime, timezone

from services.news.news_service import NewsService
from tnt_alerts.gates.news_blackout import news_blackout


class FakeRedis:
    def __init__(self):
        self.kv = {}

    def set(self, key, value):
        self.kv[key] = value
        return True

    def get(self, key):
        return self.kv.get(key)


def test_news_blackout_ignores_seen_stamp_but_honors_broadcast_stamp():
    r = FakeRedis()
    svc = NewsService(r)

    now = datetime(2026, 1, 17, 14, 5, tzinfo=timezone.utc)
    seen = datetime(2026, 1, 17, 14, 4, tzinfo=timezone.utc)

    # Seen/ingest stamp should not trigger the gate.
    svc.mark_symbol_seen_with_title("SPY", ts_utc=seen, title="seen")
    res = news_blackout(
        "SPY",
        svc,
        now_utc=now,
        symbol_minutes=3,
        market_minutes=5,
    )
    assert res.ok is True

    # Broadcast/gate stamp should trigger the gate.
    svc.mark_symbol_headline_with_title("SPY", ts_utc=seen, title="broadcast")
    res2 = news_blackout(
        "SPY",
        svc,
        now_utc=now,
        symbol_minutes=3,
        market_minutes=5,
    )
    assert res2.ok is False
    assert (res2.code or "").strip() == "SKIP_NEWS_RECENT"
