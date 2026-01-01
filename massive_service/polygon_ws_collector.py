"""Polygon WebSocket tick collector.

Purpose
- Maintain a low-latency cache of the latest tick per symbol in SQLite.
- Provide a resilient fallback story: if WS is down, existing REST flows still work.

Usage (example)
- Set `POLYGON_API_KEY`
- Optionally set:
  - `DB_PATH` (default: db/tnt.db)
  - `POLYGON_WS_MARKET` (default: stocks; one of: stocks, indices, options)
  - `POLYGON_WS_SYMBOLS` (comma-separated; default: SPY,QQQ,IWM)
    - `POLYGON_WS_CHANNELS` (comma-separated; default: T,Q)

Run
- `python -m massive_service.polygon_ws_collector`

This module intentionally does not print secrets.
"""

from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterable, Optional

import websockets


@dataclass(frozen=True)
class Tick:
    symbol: str
    price: float
    ts_iso: Optional[str]
    source: str


def _db_path() -> str:
    return os.getenv("DB_PATH", "db/tnt.db")


def _polygon_key() -> str:
    key = os.getenv("POLYGON_API_KEY", "").strip()
    if not key:
        raise RuntimeError("POLYGON_API_KEY not set")
    return key


def _ws_url() -> str:
    base = (os.getenv("POLYGON_WS_BASE_URL") or "wss://socket.polygon.io").rstrip("/")
    market = (os.getenv("POLYGON_WS_MARKET") or "stocks").strip().lower()
    if market not in {"stocks", "indices", "options"}:
        raise ValueError("POLYGON_WS_MARKET must be one of: stocks, indices, options")
    return f"{base}/{market}"


def _symbols() -> list[str]:
    raw = (os.getenv("POLYGON_WS_SYMBOLS") or "SPY,QQQ,IWM").strip()
    symbols = [s.strip().upper() for s in raw.split(",") if s.strip()]
    # Avoid accidental empty subscriptions.
    return symbols or ["SPY"]


def _channels() -> list[str]:
    raw = (os.getenv("POLYGON_WS_CHANNELS") or "T,Q").strip()
    channels = [c.strip().upper() for c in raw.split(",") if c.strip()]
    return channels or ["T", "Q"]


def _subscribe_params(symbols: Iterable[str], channels: Iterable[str]) -> str:
    parts: list[str] = []
    for ch in channels:
        for sym in symbols:
            parts.append(f"{ch}.{sym}")
    return ",".join(parts)


def _ensure_db(conn: sqlite3.Connection) -> None:
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
    cur.execute("CREATE INDEX IF NOT EXISTS idx_last_ticks_ts ON last_ticks(ts)")
    conn.commit()


def _ts_ms_to_iso(ts_ms: Any) -> Optional[str]:
    try:
        if ts_ms is None:
            return None
        ts_int = int(ts_ms)
        dt = datetime.fromtimestamp(ts_int / 1000.0, tz=timezone.utc)
        return dt.isoformat()
    except Exception:
        return None


def _extract_tick(event: dict[str, Any]) -> Optional[Tick]:
    if not isinstance(event, dict):
        return None

    # Polygon uses slightly different keys per stream; be defensive.
    sym = (event.get("sym") or event.get("S") or event.get("s") or "").strip().upper()
    if not sym:
        return None

    price: Optional[float] = None

    source = "polygon-ws"

    # Indices: value events (commonly channel V) may include `v` or `value`.
    if event.get("v") is not None or event.get("value") is not None:
        raw_val = event.get("v") if event.get("v") is not None else event.get("value")
        try:
            price = float(raw_val)
            if price > 0:
                source = "polygon-ws:V"
        except Exception:
            price = None

    # Trades: `p`.
    if event.get("p") is not None:
        try:
            price = float(event.get("p"))
            source = "polygon-ws:T"
        except Exception:
            price = None

    # Quotes: midpoint from bid/ask.
    if price is None:
        bid = event.get("bp")
        ask = event.get("ap")
        if bid is not None and ask is not None:
            try:
                bid_f = float(bid)
                ask_f = float(ask)
                if bid_f > 0 and ask_f > 0:
                    price = (bid_f + ask_f) / 2.0
                    source = "polygon-ws:Q"
            except Exception:
                price = None

    if price is None or price <= 0:
        return None

    ts_iso = _ts_ms_to_iso(event.get("t") or event.get("T"))
    return Tick(symbol=sym, price=float(price), ts_iso=ts_iso, source=source)


def _upsert_tick(conn: sqlite3.Connection, tick: Tick) -> None:
    recv_ts = datetime.now(timezone.utc).isoformat()
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO last_ticks(symbol, price, ts, source, recv_ts)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(symbol) DO UPDATE SET
            price=excluded.price,
            ts=excluded.ts,
            source=excluded.source,
            recv_ts=excluded.recv_ts
        """,
        (tick.symbol, tick.price, tick.ts_iso, tick.source, recv_ts),
    )
    conn.commit()


async def _run_once() -> None:
    url = _ws_url()
    symbols = _symbols()
    channels = _channels()
    sub = _subscribe_params(symbols, channels)

    with sqlite3.connect(_db_path(), timeout=30) as conn:
        _ensure_db(conn)

        async with websockets.connect(url, ping_interval=20, ping_timeout=20) as ws:
            await ws.send(json.dumps({"action": "auth", "params": _polygon_key()}))
            await ws.send(json.dumps({"action": "subscribe", "params": sub}))

            # Main loop.
            async for message in ws:
                try:
                    payload = json.loads(message)
                except Exception:
                    continue

                # Polygon streams typically send arrays of events.
                events: list[Any]
                if isinstance(payload, list):
                    events = payload
                else:
                    events = [payload]

                for ev in events:
                    tick = _extract_tick(ev) if isinstance(ev, dict) else None
                    if tick is None:
                        continue
                    try:
                        _upsert_tick(conn, tick)
                    except Exception:
                        # Do not crash the loop on transient DB issues.
                        continue


async def main_async() -> None:
    if os.getenv("POLYGON_ENABLED", "1") == "0":
        print("[polygon_ws_collector] POLYGON_ENABLED=0, exiting")
        return

    backoff = 1.0
    while True:
        try:
            await _run_once()
            backoff = 1.0
        except KeyboardInterrupt:
            print("[polygon_ws_collector] Interrupted, shutting down")
            return
        except Exception as exc:
            print(f"[polygon_ws_collector] ERROR: {exc}")
            time.sleep(backoff)
            backoff = min(backoff * 2.0, 30.0)


def main() -> None:
    asyncio.run(main_async())


if __name__ == "__main__":
    main()
