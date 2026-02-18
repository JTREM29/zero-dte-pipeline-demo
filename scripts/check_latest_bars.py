import os
import sqlite3
import datetime as dt

SYMBOLS = ["SPY", "QQQ", "IWM", "NVDA", "TSLA", "SQQQ", "VIX"]
DB_PATH = os.getenv("DB_PATH", "db/tnt.db")

con = sqlite3.connect(DB_PATH)
cur = con.cursor()
now = dt.datetime.now(dt.timezone.utc)
print("UTC now:", now.isoformat())
for sym in SYMBOLS:
    row = cur.execute(
        "SELECT max(ts) FROM prices WHERE symbol=? AND tf='1m'",
        (sym,),
    ).fetchone()
    ts = row[0] if row else None
    if not ts:
        print(f"{sym}: 1m=None")
        continue
    dt_obj = dt.datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    age_min = (now - dt_obj).total_seconds() / 60.0
    print(f"{sym}: 1m={ts} age_min={age_min:.2f}")

con.close()
