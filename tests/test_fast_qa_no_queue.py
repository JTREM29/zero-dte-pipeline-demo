import asyncio
import time

import pytest

import delivery.discord_bot as bot


@pytest.mark.asyncio
async def test_fast_mode_does_not_wait_on_analyze(monkeypatch):
    # If fast_mode accidentally calls /analyze, this would hang.
    async def _slow_analyze(symbol: str):
        await asyncio.sleep(10.0)
        return "", {}

    monkeypatch.setattr(bot, "_run_analyze_for_symbol", _slow_analyze)

    # Provide minimal ctx snapshots via a fake redis.
    class FakeRedis:
        def __init__(self):
            self._kv = {
                "ctx:market": '{"ts_utc":1700000000,"sources_present":{"futures":true,"news":false},"futures":{"hb_age_sec":10,"degraded":false}}'
            }

        def get(self, key: str):
            return self._kv.get(key)

        def exists(self, key: str) -> int:
            return 1 if key in self._kv else 0

    monkeypatch.setenv("CTX_SNAPSHOT_ENABLED", "1")
    monkeypatch.setattr(bot, "redis_client_for_ctx", lambda: FakeRedis())

    # Should return quickly (well under the 0.3s timeout) even though analyze is "slow".
    t0 = time.perf_counter()
    text, _render, status, _cache_hit, _lat = await asyncio.wait_for(
        bot.run_analyze_then_coach("SPY", "what's the market doing", fast_mode=True),
        timeout=0.3,
    )
    dt = time.perf_counter() - t0

    assert dt < 0.3
    assert isinstance(text, str) and text.strip()
    assert "queued" not in text.lower()
    assert status in {"ok", "ok_degraded", "ok_moderated", "ok_standby"}
