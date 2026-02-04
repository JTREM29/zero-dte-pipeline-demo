from __future__ import annotations

import datetime as dt
import logging

from services.calendar.earnings_embeds import build_earnings_embed


def _iso(dt_utc: dt.datetime) -> str:
    if dt_utc.tzinfo is None:
        dt_utc = dt_utc.replace(tzinfo=dt.timezone.utc)
    return dt_utc.astimezone(dt.timezone.utc).isoformat().replace("+00:00", "Z")


def test_earnings_news_filters_relevance_and_adds_tags() -> None:
    now = dt.datetime.now(dt.timezone.utc)

    ev = {
        "ts_utc": "2026-01-31T21:00:00Z",
        "confirmed": True,
        "session": "AMC",
        "expected_move_pct": 4.1,
        "company": "Apple Inc",
        "history": [{"ts_utc": "2025-10-31T20:00:00Z", "move_pct": 5.2}],
    }

    news_items = [
        {
            "headline": "AAPL beats earnings on revenue and guidance",
            "url": "https://example.com/aapl-earnings",
            "tickers": ["AAPL"],
            "published_utc": _iso(now - dt.timedelta(hours=2)),
        },
        {
            "headline": "Fed decision lifts AAPL as yields fall",
            "url": "https://example.com/fed",
            "tickers": ["AAPL"],
            "published_utc": _iso(now - dt.timedelta(hours=3)),
        },
        {
            "headline": "MSFT launches new Copilot features",
            "url": "https://example.com/msft",
            "tickers": ["MSFT"],
            "published_utc": _iso(now - dt.timedelta(hours=1)),
        },
        {
            "headline": "AAPL older headline should be filtered",
            "url": "https://example.com/old",
            "tickers": ["AAPL"],
            "published_utc": _iso(now - dt.timedelta(days=2)),
        },
    ]

    e = build_earnings_embed("AAPL", ev, refreshed_utc=None, news_items=news_items, premium=True)
    latest = next((f for f in e.fields if f.name == "Latest News"), None)
    assert latest is not None

    v = str(latest.value)
    assert "[EARNINGS]" in v
    assert "[MACRO]" in v
    assert "MSFT" not in v
    assert "older headline" not in v


def test_earnings_reality_check_field_renders_when_possible() -> None:
    ev = {
        "ts_utc": "2026-01-31T21:00:00Z",
        "confirmed": True,
        "session": "AMC",
        "expected_move_pct": 4.0,
        "history": [{"ts_utc": "2025-10-31T20:00:00Z", "move_pct": 6.0}],
    }

    e = build_earnings_embed("AAPL", ev, refreshed_utc=None, news_items=[], premium=True)
    rc = next((f for f in e.fields if f.name == "Reality check"), None)
    assert rc is not None
    s = str(rc.value)
    assert s.startswith("Reality Check:")
    assert "Last earnings realized +6.0%" in s
    assert "Implied ±4.0%" in s
    assert s.endswith("Move exceeded pricing")


def test_earnings_news_filter_emits_debug_counts_when_enabled(monkeypatch, caplog) -> None:
    monkeypatch.setenv("TNT_DEBUG_NEWS_FILTER_COUNTS", "1")
    caplog.set_level(logging.DEBUG)

    now = dt.datetime.now(dt.timezone.utc)
    ev = {
        "ts_utc": "2026-01-31T21:00:00Z",
        "confirmed": True,
        "session": "AMC",
        "expected_move_pct": 4.0,
        "history": [{"ts_utc": "2025-10-31T20:00:00Z", "move_pct": -2.0}],
    }
    news_items = [
        {"headline": "AAPL earnings preview", "tickers": ["AAPL"], "published_utc": _iso(now - dt.timedelta(hours=1))},
        {"headline": "Unrelated headline", "tickers": ["MSFT"], "published_utc": _iso(now - dt.timedelta(hours=1))},
    ]

    build_earnings_embed("AAPL", ev, refreshed_utc=None, news_items=news_items, premium=True)
    joined = "\n".join([r.getMessage() for r in caplog.records])
    assert "news_kept_count" in joined
    assert "news_filtered_out_count" in joined
