import os
import socket
import sqlite3
from datetime import datetime, timedelta

LOOKUP_HOST = "localhost"
LOOKUP_PORT = 9100

DB_PATH = os.path.join(os.path.dirname(__file__), "market_iqfeed.db")

SYMBOL = "@ES#"
INTERVAL_SEC = 60


def init_db(conn: sqlite3.Connection) -> None:
    cur = conn.cursor()
    cur.execute(
        """
    CREATE TABLE IF NOT EXISTS iq_bar_1m (
      ts TEXT NOT NULL,
      symbol TEXT NOT NULL,
      o REAL, h REAL, l REAL, c REAL,
      v REAL,
      PRIMARY KEY (ts, symbol)
    );
    """
    )
    cur.execute(
        "CREATE INDEX IF NOT EXISTS idx_iq_bar_symbol_ts ON iq_bar_1m(symbol, ts);"
    )
    conn.commit()


def send_cmd(sock: socket.socket, cmd: str) -> None:
    sock.sendall(cmd.encode("ascii"))


def recv_lines(sock: socket.socket):
    buf = b""
    while True:
        data = sock.recv(65536)
        if not data:
            break
        buf += data
        while b"\n" in buf:
            line, buf = buf.split(b"\n", 1)
            line = line.strip(b"\r").decode("latin-1", errors="ignore").strip()
            if line:
                yield line


def main() -> None:
    conn = sqlite3.connect(DB_PATH, timeout=30)
    init_db(conn)

    end = datetime.now()
    start = end - timedelta(hours=18)

    def fmt(dt: datetime) -> str:
        return dt.strftime("%Y%m%d %H%M%S")

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.connect((LOOKUP_HOST, LOOKUP_PORT))

    try:
        req = f"HIT,{SYMBOL},{INTERVAL_SEC},{fmt(start)},{fmt(end)},5000\r\n"
        send_cmd(sock, req)

        cur = conn.cursor()
        for line in recv_lines(sock):
            if line.startswith("!ENDMSG!"):
                break
            parts = line.split(",")
            if len(parts) < 6:
                continue
            ts_raw, o, h, l, c, v = parts[:6]
            ts = ts_raw.replace(" ", "T")
            cur.execute(
                "INSERT OR REPLACE INTO iq_bar_1m (ts, symbol, o, h, l, c, v) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (ts, SYMBOL, float(o), float(h), float(l), float(c), float(v)),
            )

        conn.commit()
    finally:
        try:
            sock.close()
        finally:
            conn.close()

    print("[IQFeed] Bars updated.")


if __name__ == "__main__":
    main()
