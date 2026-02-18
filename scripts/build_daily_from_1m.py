import os
import sqlite3
from datetime import date, datetime, time
from zoneinfo import ZoneInfo

DB_PATH = os.getenv("DB_PATH", "db/tnt.db")
_default_symbols = "SPY,QQQ,IWM,AAPL,MSFT,NVDA,AMZN,META,GOOGL,TSLA,VIX"
SYMBOLS = os.getenv("SYMBOLS", _default_symbols).split(",")
SYMBOLS = [s.strip().upper() for s in SYMBOLS if s.strip()]
TF_SRC = "1m"
TF_DST = "1d"

ET = ZoneInfo("America/New_York")
RTH_OPEN = time(9, 30)
RTH_CLOSE = time(16, 0)


def parse_iso(ts: str) -> datetime:
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


def main() -> None:
    with sqlite3.connect(DB_PATH) as conn:
        cur = conn.cursor()

        # Ensure index helps queries (safe if already exists)
        cur.execute("CREATE INDEX IF NOT EXISTS idx_prices_symbol_tf_ts ON prices(symbol, tf, ts)")
        conn.commit()

        price_columns = {row[1] for row in cur.execute("PRAGMA table_info(prices)")}
        has_volume = "volume" in price_columns
        has_source = "source" in price_columns
        has_is_partial = "is_partial" in price_columns

        for sym in SYMBOLS:
            # Pull a decent history chunk
            select_cols = "ts, open, high, low, close"
            if has_volume:
                select_cols += ", volume"
            cur.execute(
                """
                SELECT {cols}
                FROM prices
                WHERE symbol=? AND tf=?
                ORDER BY ts ASC
                """.format(cols=select_cols),
                (sym, TF_SRC),
            )
            rows = cur.fetchall()
            if not rows:
                print(f"[WARN] {sym}: no 1m rows")
                continue

            # group by ET date (RTH only)
            sessions: dict[str, list[tuple[datetime, float, float, float, float, float]]] = {}
            for row in rows:
                ts, o, h, l, c, *rest = row
                dt_utc = parse_iso(ts)
                dt_et = dt_utc.astimezone(ET)
                if dt_et.weekday() >= 5:
                    continue
                if not (RTH_OPEN <= dt_et.time() < RTH_CLOSE):
                    continue
                d = dt_et.date().isoformat()
                vol = float(rest[0]) if has_volume and rest and rest[0] is not None else 0.0
                sessions.setdefault(d, []).append((dt_utc, float(o), float(h), float(l), float(c), vol))

            inserted = 0
            for d, bars in sessions.items():
                bars.sort(key=lambda x: x[0])

                o = bars[0][1]
                h = max(b[2] for b in bars)
                l = min(b[3] for b in bars)
                c = bars[-1][4]
                vol_sum = sum(b[5] for b in bars) if has_volume else None

                # Use session close time as ts (16:00 ET) in UTC
                dt_date = date.fromisoformat(d)
                close_et = datetime.combine(dt_date, RTH_CLOSE, tzinfo=ET)
                close_utc = close_et.astimezone(ZoneInfo("UTC")).isoformat()

                # Upsert-style: avoid duplicates
                cols = ["symbol", "tf", "ts", "open", "high", "low", "close"]
                values = [sym, TF_DST, close_utc, o, h, l, c]
                if has_volume:
                    cols.append("volume")
                    values.append(vol_sum if vol_sum is not None else 0.0)
                if has_source:
                    cols.append("source")
                    values.append(f"{TF_DST}_from_{TF_SRC}")
                if has_is_partial:
                    cols.append("is_partial")
                    values.append(0)

                placeholders = ",".join(["?"] * len(values))
                cur.execute(
                    f"""
                    INSERT OR REPLACE INTO prices({','.join(cols)})
                    VALUES({placeholders})
                    """,
                    values,
                )
                inserted += 1

            conn.commit()
            print(f"[OK] {sym}: wrote {inserted} daily RTH bars into prices(tf='1d')")


if __name__ == "__main__":
    main()
