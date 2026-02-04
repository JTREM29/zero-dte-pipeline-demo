from __future__ import annotations

from datetime import datetime, timezone

from services.news.news_service import NewsService


class FakeRedis:
    def __init__(self):
        self.kv = {}
        self.expires = {}
        self.zsets = {}

    def set(self, key, value):
        self.kv[key] = value
        return True

    def get(self, key):
        return self.kv.get(key)

    def expire(self, key, ttl):
        self.expires[key] = int(ttl)
        return True

    def zadd(self, key, mapping):
        z = self.zsets.setdefault(key, {})
        for member, score in mapping.items():
            z[str(member)] = float(score)
        return True


def test_news_cache_updates_last_ts():
    r = FakeRedis()
    svc = NewsService(r)

    items = [
        {
            "id": "a",
            "published_utc": "2026-01-17T14:00:00+00:00",
            "headline": "A",
            "tickers": ["SPY"],
            "url": "https://x/a",
        },
        {
            "id": "b",
            "published_utc": "2026-01-17T14:05:00+00:00",
            "headline": "B",
            "tickers": ["SPY"],
            "url": "https://x/b",
        },
    ]

    stored = svc.cache_symbol_items(symbol="SPY", items=items, ttl_sec=86400)
    assert stored == 2

    last_s = svc.get_symbol_last_seen_epoch_s("SPY")
    assert last_s == int(datetime(2026, 1, 17, 14, 5, tzinfo=timezone.utc).timestamp())

    # Broadcast/gate stamp should remain unset unless explicitly marked.
    assert svc.get_symbol_last_epoch_s("SPY") is None

    assert "news:tick:SPY" in r.zsets
    assert "b" in r.zsets["news:tick:SPY"]
    assert r.get("news:item:b") is not None
