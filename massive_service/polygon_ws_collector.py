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
from pathlib import Path
import socket
from typing import Any, Iterable, Optional

import websockets

from services.observability.feed_heartbeat import write_feed_heartbeat


_WS_LOCK: object | None = None


def _acquire_singleton_lock() -> bool:
    """Best-effort single-instance lock.

    Prevents accidentally running multiple WS collectors that write concurrently
    to the same SQLite DB.

    Override with POLYGON_WS_ALLOW_MULTI=1.
    """

    allow_multi = str(os.getenv("POLYGON_WS_ALLOW_MULTI", "0") or "0").strip().lower() in {"1", "true", "yes"}
    if allow_multi:
        return True

    try:
        lock_dir = Path("logs")
        lock_dir.mkdir(parents=True, exist_ok=True)
        lock_path = lock_dir / "tnt_polygon_ws.lock"
        fh = open(lock_path, "a+", encoding="utf-8")

        # IMPORTANT (Windows): msvcrt.locking() locks from the current file
        # position. Files opened with a+ start at EOF, so two processes can
        # accidentally lock different regions and both "succeed". Always lock
        # from the start of the file.
        try:
            fh.seek(0)
        except Exception:
            pass

        # Windows: msvcrt lock (non-blocking)
        try:
            import msvcrt  # type: ignore

            try:
                msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError:
                try:
                    fh.close()
                except Exception:
                    pass
                return False
        except Exception:
            # POSIX: fcntl flock (non-blocking)
            try:
                import fcntl  # type: ignore

                try:
                    fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                except OSError:
                    try:
                        fh.close()
                    except Exception:
                        pass
                    return False
            except Exception:
                return True

        started_iso = datetime.now(timezone.utc).isoformat()
        try:
            fh.seek(0)
            fh.truncate(0)
            fh.write(f"pid={os.getpid()}\n")
            fh.write(f"host={socket.gethostname()}\n")
            fh.write(f"started_utc={started_iso}\n")
            fh.flush()
        except Exception:
            pass

        global _WS_LOCK
        _WS_LOCK = fh
        print(f"[polygon_ws_collector] singleton lock acquired pid={os.getpid()} started_utc={started_iso}")
        return True
    except Exception:
        return True


def _read_lock_owner() -> str | None:
    try:
        p = Path("logs") / "tnt_polygon_ws.lock"
        if not p.exists() or not p.is_file():
            return None
        txt = p.read_text(encoding="utf-8", errors="ignore").strip()
        return txt or None
    except Exception:
        return None


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

    write_feed_heartbeat(
        "polygon_ws",
        extra={
            "market": (os.getenv("POLYGON_WS_MARKET") or "stocks").strip().lower(),
            "symbols": len(list(symbols)),
        },
    )
    last_hb = 0.0

    with sqlite3.connect(_db_path(), timeout=30) as conn:
        _ensure_db(conn)

        async with websockets.connect(url, ping_interval=20, ping_timeout=20) as ws:
            await ws.send(json.dumps({"action": "auth", "params": _polygon_key()}))
            await ws.send(json.dumps({"action": "subscribe", "params": sub}))

            # Main loop.
            async for message in ws:
                now = time.time()
                if (now - last_hb) >= 10.0:
                    write_feed_heartbeat("polygon_ws")
                    last_hb = now
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

    if not _acquire_singleton_lock():
        owner = _read_lock_owner() or "unknown"
        print("[FATAL] Another polygon WS collector process is already running (singleton lock active).")
        print(f"[FATAL] lock_owner:\n{owner}")
        raise SystemExit(2)

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
            write_feed_heartbeat("polygon_ws", last_error=f"{type(exc).__name__}: {exc}")
            time.sleep(backoff)
            backoff = min(backoff * 2.0, 30.0)


def main() -> None:
    asyncio.run(main_async())


if __name__ == "__main__":
    main()
