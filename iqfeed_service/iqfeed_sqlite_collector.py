import os
import socket
import sqlite3
import time
from datetime import datetime

HOST = "localhost"     # handles IPv4/IPv6 on Windows
PORT = 5009

SYMBOLS = ["@ES#", "SPX"]
DB_PATH = os.path.join(os.path.dirname(__file__), "market_iqfeed.db")

RECONNECT_DELAY = 3
SOCKET_TIMEOUT = 10
WRITE_EVERY_SEC = 1.0


def iso_now():
    return datetime.now().astimezone().isoformat(timespec="seconds")


def init_db(conn: sqlite3.Connection):
    cur = conn.cursor()
    cur.execute(
        """
    CREATE TABLE IF NOT EXISTS iq_quote (
      ts TEXT NOT NULL,
      symbol TEXT NOT NULL,
      last REAL,
      bid REAL,
      ask REAL,
      PRIMARY KEY (ts, symbol)
    );
    """
    )
    cur.execute(
        "CREATE INDEX IF NOT EXISTS idx_iq_quote_symbol_ts ON iq_quote(symbol, ts);"
    )
    conn.commit()


def safe_float(x):
    try:
        return float(x)
    except Exception:
        return None


def upsert_quote(conn, symbol, last, bid, ask, ts):
    cur = conn.cursor()
    cur.execute(
        "INSERT OR REPLACE INTO iq_quote (ts, symbol, last, bid, ask) VALUES (?, ?, ?, ?, ?)",
        (ts, symbol, last, bid, ask),
    )
    conn.commit()


def connect_socket():
    s = socket.socket(socket.AF_INET6 if ":" in HOST else socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(SOCKET_TIMEOUT)
    s.connect((HOST, PORT))
    return s


def main():
    conn = sqlite3.connect(DB_PATH, timeout=30)
    init_db(conn)

    buf = b""
    last_write = 0.0

    try:
        while True:
            s = None
            try:
                s = connect_socket()
                print(f"[IQFeed] Connected to {HOST}:{PORT}")
                buf = b""

                def send(cmd: str):
                    s.sendall(cmd.encode("ascii"))

                send("S,SET PROTOCOL,6.1\r\n")
                send("S,SELECT UPDATE FIELDS,Symbol,Last,Bid,Ask\r\n")
                send("S,SET CLIENT NAME,TNT_IQFEED_SQLITE\r\n")

                for sym in SYMBOLS:
                    send(f"w{sym}\r\n")
                print(f"[IQFeed] Subscribed: {', '.join(SYMBOLS)}")

                while True:
                    data = s.recv(65536)
                    if not data:
                        raise RuntimeError("Socket closed")
                    buf += data

                    while b"\n" in buf:
                        line, buf = buf.split(b"\n", 1)
                        line = line.strip(b"\r").decode("latin-1", errors="ignore").strip()
                        if not line:
                            continue

                        parts = line.split(",")
                        if len(parts) < 3:
                            continue

                        if len(parts[0]) <= 2 and parts[0].isalpha():
                            sym = parts[1].strip()
                            last = safe_float(parts[2].strip()) if len(parts) >= 3 else None
                            bid = safe_float(parts[3].strip()) if len(parts) >= 4 else None
                            ask = safe_float(parts[4].strip()) if len(parts) >= 5 else None
                        else:
                            sym = parts[0].strip()
                            last = safe_float(parts[1].strip()) if len(parts) >= 2 else None
                            bid = safe_float(parts[2].strip()) if len(parts) >= 3 else None
                            ask = safe_float(parts[3].strip()) if len(parts) >= 4 else None

                        now = time.time()
                        if now - last_write >= WRITE_EVERY_SEC:
                            ts = iso_now()
                            upsert_quote(conn, sym, last, bid, ask, ts)
                            last_write = now

            except KeyboardInterrupt:
                print("[IQFeed] Exiting.")
                return
            except Exception as e:
                print(f"[IQFeed] ERROR: {e!r} - reconnecting in {RECONNECT_DELAY}s")
                time.sleep(RECONNECT_DELAY)
            finally:
                try:
                    if s:
                        s.close()
                except Exception:
                    pass
    finally:
        conn.close()


if __name__ == "__main__":
    main()
