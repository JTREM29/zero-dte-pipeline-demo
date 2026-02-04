import asyncio

import pytest

import delivery.discord_bot as bot


@pytest.mark.asyncio
async def test_cheap_mode_never_calls_llm(monkeypatch):
    async def _boom(*_a, **_k):
        raise AssertionError("LLM was called")

    # Tripwire: cheap mode must never call the coach.
    if hasattr(bot, "call_tnt_agent_async"):
        monkeypatch.setattr(bot, "call_tnt_agent_async", _boom)

    # Ensure ctx snapshots are disabled to avoid Redis dependency.
    monkeypatch.setenv("CTX_SNAPSHOT_ENABLED", "0")

    # No Polygon key present; the snapshot should degrade gracefully.
    monkeypatch.delenv("POLYGON_API_KEY", raising=False)

    text, _render, status, _cache_hit, _lat = await asyncio.wait_for(
        bot.run_analyze_then_coach("AAPL", "AAPL", cheap_mode=True),
        timeout=1.0,
    )

    assert isinstance(text, str) and text.strip()
    assert status in {"ok", "ok_degraded"}
    assert "llm" not in text.lower()  # user-facing should not mention llm


@pytest.mark.asyncio
async def test_cheap_mode_enriches_news_and_earnings(monkeypatch):
    # Tripwire: cheap mode must never call the coach.
    async def _boom(*_a, **_k):
        raise AssertionError("LLM was called")

    if hasattr(bot, "call_tnt_agent_async"):
        monkeypatch.setattr(bot, "call_tnt_agent_async", _boom)

    monkeypatch.setenv("CTX_SNAPSHOT_ENABLED", "0")
    monkeypatch.setenv("CHEAP_QA_ENRICH", "1")
    monkeypatch.setenv("CHEAP_QA_ENRICH_NEWS", "1")
    monkeypatch.setenv("CHEAP_QA_ENRICH_EARNINGS", "1")
    monkeypatch.setenv("MASSIVE_API_KEY", "x")
    monkeypatch.setenv("EARNINGS_API_KEY", "x")

    # Avoid Polygon key requirements by stubbing polygon_snapshot.
    def _fake_polygon_snapshot(symbol: str):
        return {
            "ticker": {
                "day": {"o": 100, "h": 105, "l": 99, "c": 104, "v": 123456, "vw": 103.2, "t": 1738080000000},
                "prevDay": {"c": 100, "t": 1737993600000},
                "lastTrade": {"p": 104, "t": 1738080000000},
            }
        }

    import delivery.on_demand_data as odd

    monkeypatch.setattr(odd, "polygon_snapshot", _fake_polygon_snapshot)

    # Stub Massive news.
    from services.news.massive_benzinga_news import NormalizedNewsItem
    from datetime import datetime, timezone

    async def _fake_fetch_news(**_kw):
        return [
            NormalizedNewsItem(
                id="1",
                published_utc=datetime.now(timezone.utc),
                headline="Test headline",
                tickers=["AAPL"],
                url="https://example.com/news",
            )
        ]

    import services.news.massive_benzinga_news as mbn

    monkeypatch.setattr(mbn, "fetch_benzinga_news", _fake_fetch_news)

    # Stub earnings.
    from services.calendar.massive_benzinga_earnings import NormalizedEarningsItem

    async def _fake_fetch_earnings(**_kw):
        return [
            NormalizedEarningsItem(
                id="e1",
                symbol="AAPL",
                ts_utc=datetime(2026, 1, 29, 13, 10, tzinfo=timezone.utc),
                confirmed=True,
            )
        ]

    import services.calendar.massive_benzinga_earnings as mbe

    monkeypatch.setattr(mbe, "fetch_benzinga_earnings", _fake_fetch_earnings)

    text, _render, status, _cache_hit, _lat = await asyncio.wait_for(
        bot.run_analyze_then_coach("AAPL", "AAPL", cheap_mode=True),
        timeout=1.0,
    )

    assert status in {"ok", "ok_degraded"}
    assert "Test headline" in text
    assert "Next earnings" in text


@pytest.mark.asyncio
async def test_cheap_mode_futures_snapshot_includes_posture_line(monkeypatch):
    # Tripwire: CHEAP_QA should still not call the coach by default.
    async def _boom(*_a, **_k):
        raise AssertionError("LLM was called")

    if hasattr(bot, "call_tnt_agent_async"):
        monkeypatch.setattr(bot, "call_tnt_agent_async", _boom)

    monkeypatch.setenv("CTX_SNAPSHOT_ENABLED", "0")
    monkeypatch.setenv("CHEAP_QA_ENRICH", "1")
    monkeypatch.setenv("CHEAP_QA_ENRICH_FUTURES", "1")

    # Stub FuturesStore to avoid Redis + Databento dependencies.
    import time
    from types import SimpleNamespace

    class _FakeStore:
        def __init__(self, *_a, **_k):
            pass

        def get_quotes_with_fallback(self, syms):
            # Match the minimal attributes used by CHEAP_QA.
            return {
                "ES": SimpleNamespace(
                    sym="ES",
                    px=5000.0,
                    chg=+10.0,
                    chg_pct=+0.20,
                    ts_utc=int(time.time()),
                    source="stub",
                )
            }

        def get_scores(self):
            # Fresh, minimal scores dict compatible with futures_context_line.coerce_scores.
            return {
                "trend": {"ES": 0.6},
                "impulse": {"NQ": 0.55},
                "vol_mult": 1.1,
                "breadth_bearish": 1,
                "breadth_total": 3,
                "regime": "RISK_ON",
                "updated_utc": int(time.time()),
            }

    import services.futures.futures_store as fs

    monkeypatch.setattr(fs, "FuturesStore", _FakeStore)

    text, _render, status, _cache_hit, _lat = await asyncio.wait_for(
        bot.run_analyze_then_coach("ES", "ES", cheap_mode=True),
        timeout=1.0,
    )

    assert status in {"ok", "ok_degraded"}
    assert "TNT Snapshot" in text
    assert "📊 Futures:" in text
