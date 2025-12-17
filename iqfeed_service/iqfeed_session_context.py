import os
import sqlite3
from datetime import datetime, time as dtime

DB_PATH = os.path.join(os.path.dirname(__file__), "market_iqfeed.db")
SYMBOL = "@ES#"


def init_db(conn: sqlite3.Connection) -> None:
    cur = conn.cursor()
    cur.execute(
        """
    CREATE TABLE IF NOT EXISTS iq_session_context (
      session_date TEXT PRIMARY KEY,
      on_high REAL, on_low REAL, on_vwap REAL,
      rth_high REAL, rth_low REAL,
      prior_rth_high REAL, prior_rth_low REAL, prior_close REAL,
      computed_ts TEXT
    );
    """
    )
    conn.commit()


def main() -> None:
    conn = sqlite3.connect(DB_PATH, timeout=30)
    init_db(conn)
    cur = conn.cursor()

    today = datetime.now().date()
    session_date = today.isoformat()

    on_start = datetime.combine(today, dtime(0, 0))
    rth_start = datetime.combine(today, dtime(9, 30))
    rth_end = datetime.combine(today, dtime(16, 0))

    def iso(dt: datetime) -> str:
        return dt.isoformat(timespec="seconds")

    cur.execute(
        """
      SELECT ts, o, h, l, c, v
      FROM iq_bar_1m
      WHERE symbol=? AND ts >= ? AND ts < ?
      ORDER BY ts ASC
    """,
        (SYMBOL, iso(on_start), iso(rth_end)),
    )
    rows = cur.fetchall()

    if not rows:
        print("[IQFeed] No bars found for session yet.")
        conn.close()
        return

    on_h = None
    on_l = None
    vwap_num = 0.0
    vwap_den = 0.0

    rth_h = None
    rth_l = None

    for ts, o, h, l, c, v in rows:
        dt = datetime.fromisoformat(ts)
        if dt < rth_start:
            on_h = h if on_h is None else max(on_h, h)
            on_l = l if on_l is None else min(on_l, l)
            vwap_num += c * v
            vwap_den += v
        else:
            rth_h = h if rth_h is None else max(rth_h, h)
            rth_l = l if rth_l is None else min(rth_l, l)

    on_vwap = (vwap_num / vwap_den) if vwap_den > 0 else None

    prior = today.fromordinal(today.toordinal() - 1)
    prior_rth_start = datetime.combine(prior, dtime(9, 30))
    prior_rth_end = datetime.combine(prior, dtime(16, 0))

    cur.execute(
        """
      SELECT MAX(h), MIN(l)
      FROM iq_bar_1m
      WHERE symbol=? AND ts >= ? AND ts < ?
    """,
        (SYMBOL, prior_rth_start.isoformat(), prior_rth_end.isoformat()),
    )
    prior_rth_h, prior_rth_l = cur.fetchone()

    cur.execute(
        """
      SELECT c
      FROM iq_bar_1m
      WHERE symbol=? AND ts < ?
      ORDER BY ts DESC LIMIT 1
    """,
        (SYMBOL, prior_rth_end.isoformat()),
    )
    prior_close_row = cur.fetchone()
    prior_close = prior_close_row[0] if prior_close_row else None

    cur.execute(
        """
      INSERT OR REPLACE INTO iq_session_context
      (session_date, on_high, on_low, on_vwap, rth_high, rth_low,
       prior_rth_high, prior_rth_low, prior_close, computed_ts)
      VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """,
        (
            session_date,
            on_h,
            on_l,
            on_vwap,
            rth_h,
            rth_l,
            prior_rth_h,
            prior_rth_l,
            prior_close,
            datetime.now().astimezone().isoformat(timespec="seconds"),
        ),
    )

    conn.commit()
    conn.close()
    print("[IQFeed] Session context updated.")


if __name__ == "__main__":
    main()
