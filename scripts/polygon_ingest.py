import os
import time
import sqlite3
import requests
from datetime import datetime, timezone, timedelta

from dotenv import load_dotenv


load_dotenv()

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
        raise RuntimeError("Missing POLYGON_API_KEY in environment")

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
    print(f"✅ Ingest starting. Symbols={SYMBOLS} TF={TF} interval={INTERVAL_SEC}s lookback={LOOKBACK_MIN}m")
    while True:
        cycle_start = utc_now()
        start = cycle_start - timedelta(minutes=LOOKBACK_MIN)
        end = cycle_start + timedelta(minutes=1)

        with sqlite3.connect(DB_PATH) as conn:
            for sym in SYMBOLS:
                try:
                    api_symbol = SYMBOL_API_MAP.get(sym, sym)
                    bars = polygon_aggs_1m(api_symbol, start, end)
                    n = upsert_bars(conn, sym, bars, source="polygon")
                    print(f"{iso(utc_now())} | {sym} | fetched={len(bars)} inserted={n}")
                except Exception as exc:
                    print(f"{iso(utc_now())} | {sym} | ERROR: {exc}")

        elapsed = (utc_now() - cycle_start).total_seconds()
        sleep_for = max(1, INTERVAL_SEC - int(elapsed))
        time.sleep(sleep_for)
    

if __name__ == "__main__":
    main()
