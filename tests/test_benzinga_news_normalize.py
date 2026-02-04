from __future__ import annotations

from datetime import datetime, timezone

import pytest

from services.news.massive_benzinga_news import fetch_benzinga_news


class _FakeResp:
    def __init__(self, payload, status: int = 200):
        self._payload = payload
        self.status = status

    async def json(self, content_type=None):  # noqa: ARG002
        return self._payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):  # noqa: ARG002
        return False


class _FakeSession:
    def __init__(self, payload):
        self._payload = payload

    def get(self, url, params=None, headers=None):  # noqa: ARG002
        return _FakeResp(self._payload)

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):  # noqa: ARG002
        return False


@pytest.mark.asyncio
async def test_benzinga_news_normalize(monkeypatch):
    payload = {
        "results": [
            {
                "id": "n1",
                "headline": "Fed headline",
                "published_utc": "2026-01-17T14:00:00Z",
                "tickers": ["SPY", "QQQ"],
                "url": "https://example.com/1",
                "summary": "hello",
            }
        ]
    }

    import aiohttp

    monkeypatch.setattr(aiohttp, "ClientSession", lambda timeout=None: _FakeSession(payload))

    out = await fetch_benzinga_news(
        base_url="https://api.massive.com",
        api_key="k",
        tickers=["SPY"],
        published_since_utc=datetime(2026, 1, 17, tzinfo=timezone.utc),
        limit=5,
    )

    assert len(out) == 1
    it = out[0]
    assert it.id == "n1"
    assert it.headline == "Fed headline"
    assert it.published_utc.tzinfo is not None
    assert "SPY" in it.tickers
    assert it.url.startswith("https://")
