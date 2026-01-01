import sqlite3

import delivery.discord_bot as bot


def test_market_session_question_classifier():
    assert bot._is_market_session_question("is the market open?") is True
    assert bot._is_market_session_question("market status") is True
    assert bot._is_market_session_question("hello") is False


def test_market_session_answer_format():
    txt = bot._answer_market_session_question()
    assert isinstance(txt, str)
    assert txt.startswith("Answer:")


def test_watchlist_question_classifier():
    assert bot._is_watchlist_question("what's on the watchlist") is True
    assert bot._is_watchlist_question("what are we watching") is True
    assert bot._is_watchlist_question("random") is False


def test_watchlist_answer_reads_db(tmp_path, monkeypatch):
    db_path = tmp_path / "tnt.db"
    monkeypatch.setattr(bot, "DB_PATH", str(db_path))

    with sqlite3.connect(str(db_path)) as conn:
        conn.execute("CREATE TABLE IF NOT EXISTS watchlist (symbol TEXT PRIMARY KEY, added_ts TEXT NOT NULL)")
        conn.execute("INSERT OR REPLACE INTO watchlist(symbol, added_ts) VALUES (?, ?)", ("SPY", "2025-01-01T00:00:00Z"))
        conn.commit()

    txt = bot._answer_watchlist_question()
    assert txt.startswith("Answer:")
    assert "SPY" in txt


def test_key_levels_answer_requires_health_ok():
    tnt_state = {
        "meta": {"data_health": "STALE"},
        "permissions": {"no_trade": False},
        "price": {"last": 100.0},
        "levels": {"pivots_rth": {"P": 99.0, "S1": 98.0, "R1": 101.0}},
    }
    assert bot._answer_key_levels_question("SPY", tnt_state) is None


def test_key_levels_answer_happy_path():
    tnt_state = {
        "meta": {"data_health": "OK", "data_freshness_sec": 12},
        "permissions": {"no_trade": False},
        "price": {"last": 100.0},
        "levels": {"pivots_rth": {"P": 99.0, "S1": 98.0, "R1": 101.0}},
        "technicals": {},
    }
    txt = bot._answer_key_levels_question("SPY", tnt_state)
    assert isinstance(txt, str)
    assert txt.startswith("Answer:")
    assert "Bullish only if:" in txt
    assert "Bearish if:" in txt
    assert "Invalidation:" in txt
    assert "Do nothing if:" in txt
