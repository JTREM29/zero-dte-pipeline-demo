from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

import pytest


def _ensure_last_ticks(conn: sqlite3.Connection) -> None:
    cur = conn.cursor()
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS last_ticks (
            symbol TEXT PRIMARY KEY,
            price REAL,
            ts TEXT,
            source TEXT,
            recv_ts TEXT
        )
        """
    )
    conn.commit()


def test_get_index_snapshot_prefers_ws_cache(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    db_path = tmp_path / "tnt.db"
    monkeypatch.setenv("DB_PATH", str(db_path))

    # Ensure REST index snapshot isn't required.
    import delivery.on_demand_data as odd

    def _boom(_symbol: str):
        raise RuntimeError("network disabled")

    monkeypatch.setattr(odd, "polygon_index_snapshot", _boom)

    now = datetime.now(timezone.utc).isoformat()
    with sqlite3.connect(str(db_path)) as conn:
        _ensure_last_ticks(conn)
        conn.execute(
            "INSERT INTO last_ticks(symbol, price, ts, source, recv_ts) VALUES(?,?,?,?,?)",
            ("I:SPX", 5678.0, now, "polygon-ws", now),
        )
        conn.commit()

    from delivery.market_data_adapter import get_index_snapshot

    snap = get_index_snapshot("SPX")
    assert snap["symbol"] == "SPX"
    assert snap["last"] == 5678.0
    assert snap["ts_iso"] == now
    assert snap["source"]["detail"].startswith("ws-cache:")
