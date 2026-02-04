import asyncio
import time

import pytest

import delivery.discord_bot as bot


@pytest.mark.asyncio
async def test_fast_qa_never_touches_heavy_paths(monkeypatch):
    # Tripwires: if any heavy path is invoked, the test should fail fast.
    def _boom(*_a, **_k):
        raise AssertionError("FAST_QA touched a heavy path")

    # Core heavy path in this module.
    if hasattr(bot, "_run_analyze_for_symbol"):
        monkeypatch.setattr(bot, "_run_analyze_for_symbol", _boom)

    # Polygon/options semaphores should never be acquired.
    for sem_name in (
        "_POLYGON_OPTIONS_HTTP_SEM",
        "_POLYGON_STOCK_HTTP_SEM",
        "_POLYGON_OPTIONS_WS_SEM",
    ):
        sem = getattr(bot, sem_name, None)
        if sem is not None and hasattr(sem, "acquire"):
            monkeypatch.setattr(sem, "acquire", _boom)

    # Worker health gate should never be touched.
    try:
        import controller.worker_health as wh

        if hasattr(wh, "WorkerHealth") and hasattr(wh.WorkerHealth, "begin_request"):
            monkeypatch.setattr(wh.WorkerHealth, "begin_request", _boom)
    except Exception:
        pass

    # SMB/job submission should never happen.
    try:
        import controller.enqueue_and_wait as ew

        if hasattr(ew, "enqueue_job"):
            monkeypatch.setattr(ew, "enqueue_job", _boom)
    except Exception:
        pass

    # Redis ctx can be minimal.
    class FakeRedis:
        def get(self, key: str):
            if key == "ctx:market":
                return '{"ts_utc":1700000000,"sources_present":{"futures":true},"futures":{"hb_age_sec":2,"degraded":false}}'
            return None

        def exists(self, key: str) -> int:
            return 1 if key == "ctx:market" else 0

    monkeypatch.setenv("CTX_SNAPSHOT_ENABLED", "1")
    monkeypatch.setattr(bot, "redis_client_for_ctx", lambda: FakeRedis())

    text, _render, status, _cache_hit, _lat = await asyncio.wait_for(
        bot.run_analyze_then_coach("SPY", "Is futures risk-on or risk-off?", fast_mode=True),
        timeout=0.5,
    )

    assert isinstance(text, str) and text.strip()
    assert status in {"ok", "ok_degraded", "ok_moderated", "ok_standby"}


@pytest.mark.asyncio
async def test_fast_qa_responds_when_redis_is_down(monkeypatch):
    # Ensure no sleeping/retry behavior.
    monkeypatch.setattr(time, "sleep", lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("sleep called")))

    def _redis_down():
        raise ConnectionError("redis down")

    monkeypatch.setenv("CTX_SNAPSHOT_ENABLED", "1")
    monkeypatch.setattr(bot, "redis_client_for_ctx", _redis_down)

    t0 = time.perf_counter()
    text, _render, status, _cache_hit, _lat = await asyncio.wait_for(
        bot.run_analyze_then_coach("SPY", "What's the regime right now?", fast_mode=True),
        timeout=0.5,
    )
    dt = time.perf_counter() - t0

    assert dt < 0.5
    assert isinstance(text, str) and text.strip()
    assert "queued" not in text.lower()
    assert status in {"ok_degraded", "ok", "ok_standby", "ok_moderated"}
