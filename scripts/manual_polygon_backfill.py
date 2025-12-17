import os
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from dotenv import load_dotenv

if __package__:
    from . import polygon_ingest  # type: ignore
else:  # pragma: no cover - direct execution fallback
    import sys

    sys.path.append(str(Path(__file__).resolve().parent.parent))
    from scripts import polygon_ingest

load_dotenv()

DB_PATH = os.getenv("DB_PATH", "db/tnt.db")
LOOKBACK_DAYS = int(os.getenv("MANUAL_BACKFILL_DAYS", "3"))


def run_backfill(days: int = LOOKBACK_DAYS) -> None:
    now = datetime.now(timezone.utc)
    start = now - timedelta(days=days)
    end = now + timedelta(minutes=1)

    symbols = polygon_ingest.SYMBOLS
    print(f"[BACKFILL] symbols={symbols} days={days} tf={polygon_ingest.TF}")

    with sqlite3.connect(DB_PATH) as conn:
        for sym in symbols:
            api_symbol = polygon_ingest.SYMBOL_API_MAP.get(sym, sym)
            try:
                bars = polygon_ingest.polygon_aggs_1m(api_symbol, start, end)
                inserted = polygon_ingest.upsert_bars(conn, sym, bars, source="polygon_backfill")
                print(f"{sym}: fetched={len(bars)} inserted={inserted}")
            except Exception as exc:  # noqa: BLE001
                print(f"{sym}: ERROR {exc}")


if __name__ == "__main__":
    run_backfill()
