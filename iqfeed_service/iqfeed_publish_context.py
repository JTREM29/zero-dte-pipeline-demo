import json
import os
import sqlite3
from datetime import datetime

DB_PATH = os.path.join(os.path.dirname(__file__), "market_iqfeed.db")
OUT_DIR = os.path.join(os.path.dirname(__file__), "output")
OUT_FILE = os.path.join(OUT_DIR, "futures_context.json")


def main() -> None:
    os.makedirs(OUT_DIR, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=30)
    cur = conn.cursor()

    try:
        today = datetime.now().date().isoformat()
        cur.execute("SELECT * FROM iq_session_context WHERE session_date=?", (today,))
        row = cur.fetchone()
        cols = [desc[0] for desc in cur.description] if cur.description else []

        payload: dict[str, object] = {
            "ts": datetime.now().astimezone().isoformat(timespec="seconds"),
            "session_date": today,
            "context": {},
        }

        if row:
            payload["context"] = dict(zip(cols, row))
            payload["ok"] = True
        else:
            payload["ok"] = False
            payload["reason"] = "No session context yet."

        tmp = OUT_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2)
        os.replace(tmp, OUT_FILE)
        print(f"[IQFeed] Wrote {OUT_FILE}")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
