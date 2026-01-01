from __future__ import annotations

import os
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


def test_fetch_live_price_prefers_ws_cache(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    db_path = tmp_path / "tnt.db"
    monkeypatch.setenv("DB_PATH", str(db_path))

    # Ensure no network call is needed: force Polygon REST snapshot to fail.
    import delivery.on_demand_data as odd

    def _boom(_symbol: str):
        raise RuntimeError("network disabled")

    monkeypatch.setattr(odd, "polygon_snapshot", _boom)

    now = datetime.now(timezone.utc).isoformat()
    with sqlite3.connect(str(db_path)) as conn:
        _ensure_last_ticks(conn)
        conn.execute(
            "INSERT INTO last_ticks(symbol, price, ts, source, recv_ts) VALUES(?,?,?,?,?)",
            ("SPY", 123.45, now, "polygon-ws", now),
        )
        conn.commit()

    price, ts_iso, src = odd.fetch_live_price("SPY")
    assert price == 123.45
    assert ts_iso == now
    assert src.startswith("ws-cache:")
