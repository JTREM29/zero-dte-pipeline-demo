from __future__ import annotations

import datetime as dt

from services.calendar.earnings_embeds import build_earnings_embed


def test_earnings_embed_renders_last4_reactions_and_reality_check_regression() -> None:
    ev = {
        "ts_utc": "2026-01-29T21:00:00Z",
        "confirmed": True,
        "session": "AMC",
        "expected_move_pct": 4.5,
        "history": [
            {"ts_utc": "2025-10-30T21:00:00Z", "session": "AMC", "gap_pct": 2.1, "move_pct": -0.4, "tag": "gap, fade"},
            {"ts_utc": "2025-07-31T21:00:00Z", "session": "AMC", "gap_pct": 1.6, "move_pct": -2.5, "tag": "gap, fade"},
            {"ts_utc": "2025-05-01T21:00:00Z", "session": "AMC", "gap_pct": -3.4, "move_pct": -3.7, "tag": "gap, continuation"},
            {"ts_utc": "2025-01-30T21:00:00Z", "session": "AMC", "gap_pct": 4.0, "move_pct": -0.7, "tag": "gap, fade"},
        ],
        "_reactions_cache": {"source": "cached_history", "updated_utc": dt.datetime.now(dt.timezone.utc).isoformat()},
    }

    assert isinstance(ev.get("history"), list)
    assert len(ev["history"]) == 4
    assert any(isinstance(x, dict) and x.get("move_pct") is not None for x in ev["history"])

    e = build_earnings_embed("AAPL", ev, refreshed_utc=None, news_items=[], premium=True)

    last4 = next((f for f in e.fields if f.name == "Last 4 earnings reactions"), None)
    assert last4 is not None
    v = str(last4.value)
    assert "Unavailable" not in v
    assert len([ln for ln in v.split("\n") if ln.strip().startswith("•")]) == 4

    rc = next((f for f in e.fields if f.name == "Reality check"), None)
    assert rc is not None
    s = str(rc.value)
    assert s.startswith("Reality Check:")
    assert "Last earnings realized" in s
