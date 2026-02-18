import os
import sqlite3
from datetime import datetime, timezone
from dotenv import load_dotenv

load_dotenv()

DB_PATH = os.getenv("DB_PATH", "./db/tnt.db")

def main():
    ts = datetime.now(timezone.utc).isoformat()

    with sqlite3.connect(DB_PATH) as conn:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO signals (
                symbol,
                ts,
                horizon_min,
                prob_up,
                prob_down,
                model_version,
                meta
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "SPY",
                ts,
                5,
                0.55,
                0.45,
                "seed-v0",
                '{"note":"manual seed for Discord test"}'
            )
        )
        conn.commit()

    print(f"✅ Seeded test signal for SPY at {ts}")

if __name__ == "__main__":
    main()
