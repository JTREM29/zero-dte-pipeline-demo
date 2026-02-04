import os
import time
import sqlite3
import requests
from datetime import datetime, timezone, timedelta
from pathlib import Path

from services.observability.feed_heartbeat import write_feed_heartbeat

from dotenv import load_dotenv


load_dotenv()


_INGEST_LOCK: object | None = None


def _acquire_singleton_lock() -> bool:
    """Best-effort single-instance lock.

    Prevents accidentally running multiple market REST ingest workers that
    write concurrently to the same SQLite DB.

    Override with POLYGON_INGEST_ALLOW_MULTI=1.
    """

    allow_multi = str(os.getenv("POLYGON_INGEST_ALLOW_MULTI", "0") or "0").strip().lower() in {"1", "true", "yes"}
    if allow_multi:
        return True

    try:
        lock_dir = Path("logs")
        lock_dir.mkdir(parents=True, exist_ok=True)
        lock_path = lock_dir / "tnt_market_ingest.lock"
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

        try:
            fh.seek(0)
            fh.truncate(0)
            db_path = os.getenv("DB_PATH", "./db/tnt.db")
            fh.write(f"pid={os.getpid()} db={db_path}\n")
            fh.flush()
        except Exception:
            pass

        global _INGEST_LOCK
        _INGEST_LOCK = fh
        return True
    except Exception:
        return True

POLYGON_API_KEY = os.getenv("POLYGON_API_KEY", "")
DB_PATH = os.getenv("DB_PATH", "./db/tnt.db")

TF = os.getenv("TF", "1m")
_default_symbols = "SPY,QQQ,IWM,AAPL,MSFT,NVDA,AMZN,META,GOOGL,TSLA,VIX"
SYMBOLS = os.getenv("SYMBOLS", _default_symbols).split(",")
SYMBOLS = [s.strip().upper() for s in SYMBOLS if s.strip()]
SYMBOL_API_MAP = {
    "VIX": "I:VIX",
}

INCLUDE_SPX = os.getenv("INCLUDE_SPX", "0") == "1"
SPX_TICKER = "I:SPX"

LOOKBACK_MIN = int(os.getenv("INGEST_LOOKBACK_MIN", "10"))
INTERVAL_SEC = int(os.getenv("INGEST_INTERVAL_SEC", "30"))
TIMEOUT_SEC = int(os.getenv("POLYGON_TIMEOUT_SEC", "15"))

if INCLUDE_SPX and SPX_TICKER not in SYMBOLS:
    SYMBOLS.append(SPX_TICKER)


def utc_now():
    return datetime.now(timezone.utc)


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


def polygon_aggs_1m(ticker: str, start: datetime, end: datetime) -> list[dict]:
    if not POLYGON_API_KEY:
        raise RuntimeError("Missing market data API key (POLYGON_API_KEY) in environment")

    start_str = start.strftime("%Y-%m-%d")
    end_str = end.strftime("%Y-%m-%d")

    url = f"https://api.polygon.io/v2/aggs/ticker/{ticker}/range/1/minute/{start_str}/{end_str}"
    params = {
        "adjusted": "true",
        "sort": "asc",
        "limit": 50000,
        "apiKey": POLYGON_API_KEY,
    }

    backoff = 1.0
    for attempt in range(1, 6):
        try:
            resp = requests.get(url, params=params, timeout=TIMEOUT_SEC)
            if resp.status_code == 429:
                time.sleep(backoff)
                backoff = min(backoff * 2, 30)
                continue
            resp.raise_for_status()
            data = resp.json()
            return data.get("results", []) or []
        except Exception:
            if attempt == 5:
                raise
            time.sleep(backoff)
            backoff = min(backoff * 2, 30)

    return []


def bar_ts_iso(bar_ms: int) -> str:
    dt = datetime.fromtimestamp(bar_ms / 1000.0, tz=timezone.utc)
    return dt.isoformat()


def bar_is_sane(prev_close: float | None, o: float, h: float, l: float, c: float) -> bool:
    if any(x <= 0 for x in [o, h, l, c]):
        return False
    if not (l <= o <= h and l <= c <= h):
        return False

    if prev_close is not None and abs(c - prev_close) / prev_close > 0.08:
        return False

    return True


def get_last_close(conn: sqlite3.Connection, symbol: str) -> float | None:
    cur = conn.cursor()
    cur.execute(
        "SELECT close FROM prices WHERE symbol=? AND tf=? ORDER BY ts DESC LIMIT 1",
        (symbol, TF),
    )
    row = cur.fetchone()
    return float(row[0]) if row else None


def upsert_bars(conn: sqlite3.Connection, symbol: str, bars: list[dict], source: str = "polygon") -> int:
    inserted = 0
    prev_close = get_last_close(conn, symbol)

    cur = conn.cursor()
    for b in bars:
        o = float(b.get("o", 0))
        h = float(b.get("h", 0))
        l = float(b.get("l", 0))
        c = float(b.get("c", 0))
        v = float(b.get("v", 0))
        ts = bar_ts_iso(int(b["t"]))

        if not bar_is_sane(prev_close, o, h, l, c):
            continue

        cur.execute(
            """
            INSERT OR IGNORE INTO prices(symbol, tf, ts, open, high, low, close, volume, source, is_partial)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0)
            """,
            (symbol, TF, ts, o, h, l, c, v, source),
        )
        if cur.rowcount == 1:
            inserted += 1
            prev_close = c

    conn.commit()
    return inserted

def main():
    if not _acquire_singleton_lock():
        print("[FATAL] Another ingest process is already running (singleton lock active).")
        raise SystemExit(2)

    print(f"[OK] Ingest starting. Symbols={SYMBOLS} TF={TF} interval={INTERVAL_SEC}s lookback={LOOKBACK_MIN}m")
    write_feed_heartbeat("polygon_rest")
    while True:
        cycle_start = utc_now()
        last_err: str | None = None
        start = cycle_start - timedelta(minutes=LOOKBACK_MIN)
        end = cycle_start + timedelta(minutes=1)

        with sqlite3.connect(DB_PATH, timeout=30) as conn:
            for sym in SYMBOLS:
                try:
                    api_symbol = SYMBOL_API_MAP.get(sym, sym)
                    bars = polygon_aggs_1m(api_symbol, start, end)
                    n = upsert_bars(conn, sym, bars, source="polygon")
                    print(f"{iso(utc_now())} | {sym} | fetched={len(bars)} inserted={n}")
                except Exception as exc:
                    last_err = f"{type(exc).__name__}: {exc}"
                    print(f"{iso(utc_now())} | {sym} | ERROR: {exc}")

        write_feed_heartbeat("polygon_rest", last_error=last_err)

        elapsed = (utc_now() - cycle_start).total_seconds()
        sleep_for = max(1, INTERVAL_SEC - int(elapsed))
        time.sleep(sleep_for)
    

if __name__ == "__main__":
    main()
