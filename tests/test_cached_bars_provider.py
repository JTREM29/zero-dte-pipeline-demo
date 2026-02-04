import pandas as pd

from technicals.cached_bars_provider import CachedBarsProvider


class _FakeRedis:
    def __init__(self):
        self.store = {}

    def get(self, key: str):
        return self.store.get(key)

    def setex(self, key: str, ttl: int, value: str):
        self.store[key] = value
        return True


class _DummyProvider:
    def __init__(self):
        self.calls = 0

    def get_bars(self, symbol: str, timeframe: str, lookback_bars: int) -> pd.DataFrame:
        self.calls += 1
        ts = pd.date_range("2026-01-01", periods=lookback_bars, freq="min")
        close = pd.Series(range(lookback_bars), dtype=float)
        return pd.DataFrame({"ts": ts, "open": close, "high": close + 1, "low": close - 1, "close": close, "volume": 1})


def test_cached_bars_provider_hit_after_miss():
    r = _FakeRedis()
    base = _DummyProvider()
    p = CachedBarsProvider(inner=base, provider_id="polygon", redis_client=r, enabled=True)

    df1 = p.get_bars("SPY", "5m", 10)
    assert len(df1) == 10
    assert base.calls == 1
    assert p.last_cache_hit is False

    df2 = p.get_bars("SPY", "5m", 10)
    assert len(df2) == 10
    assert base.calls == 1
    assert p.last_cache_hit is True


def test_cached_bars_provider_fallback_no_redis(monkeypatch):
    # Force env-backed redis init to fail so the wrapper becomes a pure pass-through.
    monkeypatch.setattr("technicals.cached_bars_provider._redis_from_env", lambda: None)

    base = _DummyProvider()
    p = CachedBarsProvider(inner=base, provider_id="polygon", redis_client=None, enabled=True)

    df1 = p.get_bars("SPY", "5m", 10)
    df2 = p.get_bars("SPY", "5m", 10)

    assert isinstance(df1, pd.DataFrame)
    assert isinstance(df2, pd.DataFrame)
    assert df1.equals(df2)
    assert base.calls == 2  # no redis => no caching
    assert getattr(p, "last_cache_hit", False) is False
    assert int(getattr(p, "last_ttl_s", 0) or 0) == 15
