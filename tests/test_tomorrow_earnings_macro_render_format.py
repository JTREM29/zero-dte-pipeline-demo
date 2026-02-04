from __future__ import annotations

from datetime import datetime, timezone

import delivery.discord_bot as bot


def test_tomorrow_earnings_macro_render_missing_sources(monkeypatch, tmp_path):
    macro_path = tmp_path / "macro_events.csv"
    monkeypatch.setattr(bot, "MACRO_EVENTS_FILE", str(macro_path), raising=False)

    monkeypatch.setattr(bot, "load_macro_events_for_date", lambda _date: [], raising=False)
    monkeypatch.setattr(bot, "fetch_earnings_for_date", lambda _date: [], raising=False)
    monkeypatch.setattr(bot, "EARNINGS_API_KEY", "", raising=False)

    now_et = datetime(2026, 1, 27, 12, 34, tzinfo=timezone.utc).astimezone(bot.ET)
    rendered = bot.build_tomorrow_earnings_macro_render(target_date_et="2026-01-28", now_et=now_et)

    text = rendered.text
    assert "Tomorrow" in text
    assert "Earnings + Macro" in text
    assert "Expected Tomorrow" in text
    assert "Macro Events" in text
    assert "Earnings" in text
    assert "Macro calendar missing" in text
    assert "missing API key" in text
    assert "Updated:" in text
    assert "Data age:" in text
    assert "Context only" in text


def test_tomorrow_earnings_macro_render_includes_sections(monkeypatch, tmp_path):
    macro_path = tmp_path / "macro_events.csv"
    macro_path.write_text("time_et,impact,title\n08:30,HIGH,CPI\n", encoding="utf-8")
    monkeypatch.setattr(bot, "MACRO_EVENTS_FILE", str(macro_path), raising=False)

    monkeypatch.setattr(
        bot,
        "load_macro_events_for_date",
        lambda _date: [
            {"time_et": "08:30", "impact": "HIGH", "title": "CPI"},
            {"time_et": "10:00", "impact": "MED", "title": "Consumer Confidence"},
        ],
        raising=False,
    )

    monkeypatch.setattr(
        bot,
        "fetch_earnings_for_date",
        lambda _date: [
            {"symbol": "MSFT", "when": "AMC"},
            {"symbol": "AAPL", "when": "BMO"},
            {"symbol": "XYZ", "when": "TAS"},
        ],
        raising=False,
    )
    monkeypatch.setattr(bot, "EARNINGS_API_KEY", "test", raising=False)

    now_et = datetime(2026, 1, 27, 19, 0, tzinfo=timezone.utc).astimezone(bot.ET)
    rendered = bot.build_tomorrow_earnings_macro_render(target_date_et="2026-01-28", now_et=now_et)

    text = rendered.text
    assert "For: 2026-01-28" in text
    assert "Macro Events (ET)" in text

    # Macro bullets
    assert "08:30" in text
    assert "CPI" in text

    # Earnings bullets
    assert "BMO:" in text
    assert "AMC:" in text
    assert "Time not supplied" in text
    assert "Updated:" in text
    assert "Data age:" in text
    assert "Context only" in text
