from __future__ import annotations

import asyncio
import json
import os
import time

from services.context.context_snapshot import build_market_context, build_symbol_context


# If no explicit symbols are configured, publish a small baseline set so
# symbol-context requests don't dead-end on missing ctx:sym:*.
_DEFAULT_CTX_SYMBOLS: tuple[str, ...] = (
    "SPY",
    "QQQ",
    "IWM",
    "VIX",
    "SPX",
    "ES",
    "NQ",
)


def _env_int(name: str, default: int) -> int:
    try:
        return int(str(os.getenv(name, str(default)) or str(default)).strip())
    except Exception:
        return default


def _env_csv(name: str) -> list[str]:
    raw = (os.getenv(name, "") or "").strip()
    if not raw:
        return []
    return [s.strip().upper() for s in raw.split(",") if s.strip()]


def _now() -> int:
    return int(time.time())


def write_context_once(store, *, symbols: list[str], stale_hours: int, ttl_sec: int | None = None) -> dict:
    syms = sorted({str(s or "").strip().upper() for s in (symbols or []) if str(s or "").strip()})

    ttl = None
    try:
        if ttl_sec is not None:
            ttl = int(ttl_sec)
    except Exception:
        ttl = None
    if ttl is not None and ttl <= 0:
        ttl = None

    def _expire(key: str) -> None:
        if ttl is None:
            return
        try:
            store.r.expire(key, int(ttl))
        except Exception:
            return

    mkt = build_market_context(store)
    store.r.set("ctx:market", json.dumps(mkt, separators=(",", ":"), ensure_ascii=False))
    _expire("ctx:market")
    store.r.set("ctx:hb", str(_now()))
    _expire("ctx:hb")

    for sym in syms:
        ctx = build_symbol_context(store, sym, stale_hours=stale_hours)
        store.r.set(f"ctx:sym:{sym}", json.dumps(ctx, separators=(",", ":"), ensure_ascii=False))
        _expire(f"ctx:sym:{sym}")

    # Last write metadata (best-effort; helpful for /context_status)
    try:
        # Writer identity helps detect competing writers.
        try:
            import socket

            host = socket.gethostname()
        except Exception:
            host = ""
        try:
            pid = int(os.getpid())
        except Exception:
            pid = 0
        try:
            store.r.set("ctx:last_writer_host", str(host or "")[:80])
            store.r.set("ctx:last_writer_pid", str(pid or 0))
            _expire("ctx:last_writer_host")
            _expire("ctx:last_writer_pid")
        except Exception:
            pass

        store.r.set("ctx:last_write_count", str(len(syms)))
        store.r.set("ctx:last_write_symbols", json.dumps(syms, separators=(",", ":"), ensure_ascii=False))
        store.r.set("ctx:last_write_ts", str(_now()))
        try:
            if hasattr(store.r, "incr"):
                store.r.incr("ctx:last_write_seq")
                _expire("ctx:last_write_seq")
        except Exception:
            pass
        _expire("ctx:last_write_count")
        _expire("ctx:last_write_symbols")
        _expire("ctx:last_write_ts")
    except Exception:
        pass

    return {"symbols": syms, "count": len(syms)}


async def context_writer_loop(store):
    enabled = (os.getenv("CONTEXT_WRITER_ENABLED", "1") or "1").strip().lower() in {"1", "true", "yes", "on"}
    if not enabled:
        return

    poll_sec = max(5, _env_int("CONTEXT_WRITER_POLL_SEC", 60))
    stale_h = _env_int("EARNINGS_PREVIEW_STALE_HOURS", 48)
    stale_h = max(1, min(int(stale_h), 168))

    log_enabled = (os.getenv("CONTEXT_WRITER_LOG", "1") or "1").strip().lower() in {"1", "true", "yes", "on"}

    while True:
        try:
            # symbols: earnings symbols + (optional) any additional configured list
            syms = set(_env_csv("EARNINGS_AUTOPOST_SYMBOLS"))
            syms.update(_env_csv("CONTEXT_WRITER_SYMBOLS"))

            # Drop-in safe default: when nothing is configured, still publish a minimal
            # set of widely-used symbols so /ask doesn't return missing snapshot.
            if not syms:
                syms.update(_DEFAULT_CTX_SYMBOLS)

            result = write_context_once(store, symbols=sorted(syms), stale_hours=stale_h)
            if log_enabled:
                try:
                    count = int(result.get("count", 0)) if isinstance(result, dict) else 0
                except Exception:
                    count = 0
                print(f"[TNT][CONTEXT][WRITE] ts={_now()} symbols={count} poll_sec={poll_sec}")

            await asyncio.sleep(float(poll_sec))
        except Exception as e:
            try:
                store.r.set("ctx:last_error", str(e)[:200])
            except Exception:
                pass
            if log_enabled:
                try:
                    print(f"[TNT][CONTEXT][ERR] ts={_now()} err={type(e).__name__}: {str(e)[:120]}")
                except Exception:
                    pass
            await asyncio.sleep(float(min(60, poll_sec)))
