import json
import sqlite3

import delivery.discord_bot as bot


def test_sanitize_edge_profile_rejects_empty():
    assert bot._sanitize_edge_profile({}) is None
    assert bot._sanitize_edge_profile("nope") is None


def test_sanitize_edge_profile_accepts_fields():
    out = bot._sanitize_edge_profile(
        {
            "risk_budget": "medium",
            "hold_time": "intraday",
            "preferred_setups": ["pivot reclaim", "trend day"],
            "avoid": ["chop", "late entries"],
            "notes": "Be conservative into events",
        }
    )
    assert isinstance(out, dict)
    assert out["risk_budget"] == "MEDIUM"
    assert out["hold_time"] == "INTRADAY"
    assert "pivot reclaim" in out["preferred_setups"]


def test_load_global_edge_profile_from_env(monkeypatch):
    monkeypatch.setenv(
        "TNT_EDGE_PROFILE_JSON",
        json.dumps({"risk_budget": "low", "preferred_setups": ["mean reversion"]}),
    )
    out = bot._load_global_edge_profile()
    assert out is not None
    assert out.get("risk_budget") == "LOW"


def test_load_global_edge_profile_from_db(tmp_path, monkeypatch):
    db_path = tmp_path / "tnt.db"
    monkeypatch.setattr(bot, "DB_PATH", str(db_path))
    monkeypatch.delenv("TNT_EDGE_PROFILE_JSON", raising=False)

    with sqlite3.connect(str(db_path)) as conn:
        bot.migrate_settings_table(conn)
        bot.settings_set(conn, "edge_profile_json", json.dumps({"hold_time": "scalp"}))

    out = bot._load_global_edge_profile()
    assert out is not None
    assert out.get("hold_time") == "SCALP"
