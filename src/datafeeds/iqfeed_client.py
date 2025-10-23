"""IQFeed connectivity utilities.

This module now contains:
 - IQFeedClient (lookup/ping/demo chain stubs)
 - IQFeedLevel1Stream: a lightweight threaded Level1 quote streamer
 - parse_level1_line: parser for raw IQFeed 'Q' lines into a dict

It does NOT perform full protocol negotiation (login, advanced field set selection),
but provides a practical starting point for real-time ingestion. The IQFeed Windows
client must be running locally and permissions for Level1 data must exist.
"""
from __future__ import annotations
from dataclasses import dataclass
from typing import Any, Optional, Callable, Dict
import os
import socket
import time
import threading
import queue

from src.utils.logging_setup import get_logger


@dataclass
class IQFeedConfig:
    username: str | None
    password: str | None
    host: str = "127.0.0.1"
    port_level1: int = 5009
    port_admin: int = 9300
    port_lookup: int = 9100  # separate lookup/derivative requests if needed
    timeout: float = 2.0

    @classmethod
    def from_env(cls) -> "IQFeedConfig":
        return cls(
            username=os.getenv("IQFEED_USERNAME"),
            password=os.getenv("IQFEED_PASSWORD"),
            host=os.getenv("IQFEED_HOST", "127.0.0.1"),
            port_level1=int(os.getenv("IQFEED_PORT_LEVEL1", "5009")),
            port_admin=int(os.getenv("IQFEED_PORT_ADMIN", "9300")),
            port_lookup=int(os.getenv("IQFEED_PORT_LOOKUP", "9100")),
            timeout=float(os.getenv("IQFEED_TIMEOUT", "2.0")),
        )


class IQFeedClient:
    def __init__(self, cfg: IQFeedConfig):
        self._cfg = cfg
        self.log = get_logger("iqfeed")
        self.host: str = cfg.host
        self.port: int = cfg.port_level1
        self.timeout: float = cfg.timeout

    # --- Low-level helper ---
    def _send_and_recv(self, msg: str, bufsize: int = 65536) -> str:
        with socket.create_connection((self.host, self.port), timeout=self.timeout) as s:
            s.sendall(msg.encode("utf-8"))
            time.sleep(0.1)  # allow data to accumulate
            data = s.recv(bufsize)
        decoded = data.decode("utf-8", errors="replace")
        return decoded

    # --- Public methods ---
    def lookup_last(self, symbol: str) -> Optional[dict[str, Any]]:
        """Fetch a naive last snapshot using watch/unwatch pattern.

        NOTE: Protocol negotiation (S,SET PROTOCOL,...) and proper message parsing
        should be expanded based on IQFeed documentation. This is a minimal stub.
        """
        try:
            # Open a transient connection to Level1 and issue watch/unwatch
            with socket.create_connection((self.host, self.port), timeout=self.timeout) as s:
                s.settimeout(self.timeout)
                # Use CRLF line endings per IQFeed protocol
                s.sendall(b"S,SET PROTOCOL,6.2\r\n")
                try:
                    s.sendall(b"S,SET CLIENT NAME,ZeroDTE-pipeline\r\n")
                    s.sendall(b"S,TIMESTAMPSOFF\r\n")
                except Exception:
                    pass
                s.sendall((f"w{symbol}\r\n").encode("utf-8"))

                buf = ""
                payload: dict[str, Any] = {"symbol": symbol, "raw": ""}
                end_time = time.time() + max(3.0, self.timeout)
                first_q: Optional[str] = None
                while time.time() < end_time:
                    try:
                        chunk = s.recv(4096)
                        if not chunk:
                            break
                        buf += chunk.decode("utf-8", errors="replace")
                        # Process complete lines
                        lines = buf.splitlines()
                        # Keep last partial line in buffer
                        if buf and not buf.endswith("\n"):
                            buf = lines[-1]
                            lines = lines[:-1]
                        else:
                            buf = ""
                        for line in lines:
                            if line.startswith("Q,") and first_q is None:
                                first_q = line
                                # We can break outer loop soon after unwatch
                                break
                        if first_q:
                            break
                    except socket.timeout:
                        # try again until end_time
                        continue
                # Unwatch using 'r' command
                try:
                    s.sendall((f"r{symbol}\r\n").encode("utf-8"))
                except Exception:
                    pass

                if first_q:
                    from .iqfeed_client import parse_level1_line, parse_level1_line_dynamic  # local import to avoid circular
                    # Prefer dynamic parser; fallback to static if dynamic fails
                    parsed = parse_level1_line_dynamic(first_q) or parse_level1_line(first_q)
                    payload["raw"] = first_q
                    if parsed:
                        payload.update({k: v for k, v in parsed.items() if k not in {"raw"}})
                        # Provide canonical last_price alias
                        lp = parsed.get("last_trade")
                        if isinstance(lp, (int, float)):
                            payload["last_price"] = lp
                    return payload
                # If no Q line received, return raw (if any)
                if buf:
                    payload["raw"] = buf
                    return payload
                return None
        except Exception as e:  # noqa: BLE001
            return None

    def ping(self) -> bool:
        try:
            from .iqfeed_sockets import IQSocket, DEFAULT_PORTS, DEFAULT_HOST
            with IQSocket(host=DEFAULT_HOST, port=DEFAULT_PORTS["level1"]) as s:
                s.send("S,SERVER CONNECTED")
                # A simple read to flush a response (may be empty on some builds, so ignore)
            return True
        except Exception:  # noqa: BLE001
            return False

    def option_nbbo(self, symbol: str, *, timeout: Optional[float] = None) -> Optional[dict[str, Any]]:
        """Fetch a best-effort option NBBO snapshot for a given OPRA/OSI symbol.

        Uses a transient Level1 connection with watch/unwatch and returns a dict:
          {"symbol", "bid", "ask", "bs", "asz", "raw"}
        Returns None on failure or if no quote line was received in time.
        """
        to = float(timeout) if (timeout is not None) else max(2.5, self.timeout)
        try:
            with socket.create_connection((self.host, self.port), timeout=to) as s:
                s.settimeout(to)
                try:
                    s.sendall(b"S,SET PROTOCOL,6.2\r\n")
                    s.sendall(b"S,SET CLIENT NAME,ZeroDTE-pipeline\r\n")
                    s.sendall(b"S,TIMESTAMPSOFF\r\n")
                except Exception:
                    pass
                s.sendall((f"w{symbol}\r\n").encode("utf-8"))
                buf = ""
                first_q: Optional[str] = None
                end_time = time.time() + to
                while time.time() < end_time:
                    try:
                        chunk = s.recv(4096)
                        if not chunk:
                            break
                        buf += chunk.decode("utf-8", errors="replace")
                        lines = buf.splitlines()
                        if buf and not buf.endswith("\n"):
                            buf = lines[-1]
                            lines = lines[:-1]
                        else:
                            buf = ""
                        for line in lines:
                            if line.startswith("Q,"):
                                first_q = line
                                break
                        if first_q:
                            break
                    except socket.timeout:
                        continue
                try:
                    s.sendall((f"r{symbol}\r\n").encode("utf-8"))
                except Exception:
                    pass
                if not first_q:
                    return None
                # Parse using dynamic fieldnames first; fall back to static map
                try:
                    parsed = parse_level1_line_dynamic(first_q)  # type: ignore[name-defined]
                except Exception:
                    parsed = None
                if not parsed:
                    try:
                        parsed = parse_level1_line(first_q)  # type: ignore[name-defined]
                    except Exception:
                        parsed = None
                if not isinstance(parsed, dict):
                    return None
                bid = parsed.get("bid"); ask = parsed.get("ask")
                bs = parsed.get("bid_size") or parsed.get("bs")
                az = parsed.get("ask_size") or parsed.get("asz")
                out: dict[str, Any] = {
                    "symbol": symbol,
                    "bid": float(bid) if isinstance(bid, (int, float)) else float(bid) if isinstance(bid, str) and bid else 0.0,
                    "ask": float(ask) if isinstance(ask, (int, float)) else float(ask) if isinstance(ask, str) and ask else 0.0,
                    "bs": int(bs) if isinstance(bs, (int, float)) else int(bs) if isinstance(bs, str) and bs.isdigit() else 0,
                    "asz": int(az) if isinstance(az, (int, float)) else int(az) if isinstance(az, str) and az.isdigit() else 0,
                    "raw": first_q,
                }
                return out
        except Exception:
            return None

    # --- Lookup helpers (symbol search via lookup port) ---
    def _lookup_open(self) -> socket.socket:
        s = socket.create_connection((self._cfg.host, self._cfg.port_lookup), timeout=self._cfg.timeout)
        s.settimeout(self._cfg.timeout)
        try:
            s.sendall(b"S,SET PROTOCOL,6.2\n")
        except Exception:
            pass
        return s

    def _collect_until_end(self, sock: socket.socket, end_marker: str = "!ENDMSG!") -> list[str]:
        lines: list[str] = []
        start = time.time()
        buf = b""
        while (time.time() - start) < self._cfg.timeout:
            try:
                chunk = sock.recv(65536)
                if not chunk:
                    break
                buf += chunk
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    text = line.decode("utf-8", errors="replace").strip()
                    if text:
                        lines.append(text)
                        if text.startswith(end_marker):
                            return lines
            except socket.timeout:
                break
            except Exception:
                break
        return lines

    def symbol_search(self, query: str) -> dict[str, Any]:  # pragma: no cover - network
        """Attempt a basic symbol search for a given query string using IQFeed lookup port.

        Tries multiple known request patterns and returns raw lines for transparency.
        """
        attempts = [
            f"S,REQUEST SYMBOLS,{query}\n",
            f"S,SYMBOL SEARCH,{query}\n",
            f"SBF,{query}\n",
        ]
        results: dict[str, Any] = {"query": query, "attempts": [], "lines": []}
        try:
            with self._lookup_open() as s:
                for cmd in attempts:
                    try:
                        s.sendall(cmd.encode("utf-8"))
                        lines = self._collect_until_end(s)
                        # ensure lists for mypy/pyright
                        if not isinstance(results.get("attempts"), list):
                            results["attempts"] = []
                        if not isinstance(results.get("lines"), list):
                            results["lines"] = []
                        attempts_list: list[str] = results["attempts"]
                        attempts_list.append(cmd.strip())
                        if lines:
                            lines_list: list[str] = list(lines)
                            results["lines"] = lines_list
                            break
                    except Exception:
                        continue
        except Exception as exc:  # noqa: BLE001
            results["error"] = str(exc)
        return results

    def fetch_demo_chain(self, root: str) -> list[dict[str, Any]]:
        # Placeholder static symbols
        return [
            {"symbol": f"{root}250925C00000000", "type": "call", "_demo": True},
            {"symbol": f"{root}250925P00000000", "type": "put", "_demo": True},
        ]

    # --- Historical daily close (lookup port) ---
    def lookup_daily_close(self, symbol: str, date_str: str) -> Optional[float]:  # pragma: no cover - network
        """Request daily data for a specific date and return the close if available.

        date_str must be in YYYYMMDD format.
        """
        try:
            with self._lookup_open() as s:
                cmd = f"HDT,{symbol},{date_str},{date_str}\n"
                s.sendall(cmd.encode("utf-8"))
                lines = self._collect_until_end(s)
                for line in lines:
                    if not line or line.startswith("!ENDMSG!"):
                        continue
                    # Expected CSV: YYYYMMDD,open,high,low,close,volume,open_interest
                    parts = [p.strip() for p in line.split(",")]
                    if len(parts) >= 5 and parts[0].isdigit():
                        try:
                            close_val = float(parts[4])
                            return close_val
                        except Exception:
                            continue
        except Exception:
            return None
        return None

    # --- Best-effort reference helpers: earnings/economic events ---
    def fetch_earnings_range(self, start_date, end_date) -> Optional[list[dict]]:
        """Best-effort earnings fetch placeholder for IQFeed.

        IQFeed provides fundamentals and news via separate services/ports. This minimal client
        doesn't implement those protocols. As a pragmatic step, optionally read from an env-provided
        JSON/JSONL file path (IQFEED_EARNINGS_JSON) to integrate local exports.
        """
        import os as _os
        from pathlib import Path as _Path
        path = _os.getenv("IQFEED_EARNINGS_JSON")
        if not path:
            self.log.debug("IQFeed earnings fetch not implemented; set IQFEED_EARNINGS_JSON to a JSON/JSONL export if available.")
            return None
        p = _Path(path)
        if not p.exists():
            self.log.warning("IQFEED_EARNINGS_JSON path does not exist: %s", path)
            return None
        try:
            txt = p.read_text(encoding="utf-8").strip()
            items: list[dict] = []
            if txt.startswith("["):
                import json as __j
                arr = __j.loads(txt)
                if isinstance(arr, list):
                    items = [x for x in arr if isinstance(x, dict)]
            else:
                import json as __j
                for ln in txt.splitlines():
                    ln = ln.strip()
                    if not ln:
                        continue
                    try:
                        obj = __j.loads(ln)
                        if isinstance(obj, dict):
                            items.append(obj)
                    except Exception:
                        continue
            return items or None
        except Exception:
            return None

    def fetch_economic_events_range(self, start_date, end_date) -> Optional[list[dict]]:
        """Best-effort economic events fetch placeholder for IQFeed.

        As above, supports reading from IQFEED_ECON_JSON env (JSON/JSONL). Returns None if unavailable.
        """
        import os as _os
        from pathlib import Path as _Path
        path = _os.getenv("IQFEED_ECON_JSON")
        if not path:
            self.log.debug("IQFeed econ fetch not implemented; set IQFEED_ECON_JSON to a JSON/JSONL export if available.")
            return None
        p = _Path(path)
        if not p.exists():
            self.log.warning("IQFEED_ECON_JSON path does not exist: %s", path)
            return None
        try:
            txt = p.read_text(encoding="utf-8").strip()
            items: list[dict] = []
            if txt.startswith("["):
                import json as __j
                arr = __j.loads(txt)
                if isinstance(arr, list):
                    items = [x for x in arr if isinstance(x, dict)]
            else:
                import json as __j
                for ln in txt.splitlines():
                    ln = ln.strip()
                    if not ln:
                        continue
                    try:
                        obj = __j.loads(ln)
                        if isinstance(obj, dict):
                            items.append(obj)
                    except Exception:
                        continue
            return items or None
        except Exception:
            return None


# ---------------------- Level1 Streaming ----------------------

LEVEL1_FIELD_MAP = {
    # Subset of IQFeed Q message (v6.2). Full spec has many more indexes; extend as needed.
    0: "msg_type",      # 'Q'
    1: "symbol",
    2: "bid",
    3: "ask",
    4: "bid_size",
    5: "ask_size",
    6: "last_trade",
    7: "last_trade_size",
    8: "last_trade_time",  # HH:MM:SS(.ms)
    9: "total_volume",
    10: "day_high",
    11: "day_low",
    12: "open",
    13: "close_yest",
    14: "bid_time",      # HH:MM:SS
    15: "ask_time",      # HH:MM:SS
    16: "trade_market_center",
    17: "bid_market_center",
    18: "ask_market_center",
    19: "day_num_trades",
    20: "reserved_0",
    21: "exchange_id",
    22: "fraction_disp",
    # indices beyond this truncated intentionally (can be extended without breaking parsing)
}

_NUMERIC_HINTS = {
    "bid", "ask", "bid_size", "ask_size", "last_trade", "last_trade_size", "total_volume", "day_high",
    "day_low", "open", "close_yest", "day_num_trades"
}


def _parse_trade_time(value: str) -> Optional[float]:
    """Parse IQFeed last trade time (HH:MM:SS[.mmm]) into epoch seconds for *today*.

    IQFeed supplies time only; we assume current local date. If parsing fails returns None.
    """
    if not value:
        return None
    try:
        parts = value.split(":")
        if len(parts) < 3:
            return None
        h, m, s_part = parts
        if "." in s_part:
            s, frac = s_part.split(".", 1)
            ms = int((frac + "000")[:3])  # pad / truncate to ms
        else:
            s = s_part
            ms = 0
        h_i = int(h); m_i = int(m); s_i = int(s)
        import datetime as _dt, time as _t
        today = _dt.date.fromtimestamp(_t.time())  # local date
        dt = _dt.datetime(today.year, today.month, today.day, h_i, m_i, s_i, ms * 1000)
        return dt.timestamp()
    except Exception:
        return None


def parse_level1_line(line: str) -> Optional[Dict[str, Any]]:
    """Parse a raw Level1 'Q' message into a dict (extended subset).

    Unknown field indexes are ignored. Adds convenience aliases:
      - last_price (alias of last_trade when present)
      - mid (if bid+ask numeric)
      - spread (ask - bid) & spread_bps
      - epoch (derived from last_trade_time local day)
    Returns None for system or malformed lines.
    """
    if not line:
        return None
    line = line.strip()
    if not line or line.startswith("S,") or not line.startswith("Q,"):
        return None
    parts = line.split(",")
    if len(parts) < 9:  # ensure we have basic time field
        return None
    data: Dict[str, Any] = {"raw": line}
    for idx, key in LEVEL1_FIELD_MAP.items():
        if idx >= len(parts):
            continue
        raw_val = parts[idx]
        if key in _NUMERIC_HINTS:
            if raw_val == "":
                data[key] = None
            else:
                try:
                    data[key] = float(raw_val)
                except ValueError:
                    data[key] = None
        else:
            data[key] = raw_val
    # Derived helpers
    bid = data.get("bid"); ask = data.get("ask")
    if isinstance(bid, (int, float)) and isinstance(ask, (int, float)) and bid > 0 and ask > 0:
        data["mid"] = (bid + ask) / 2.0
        spread = ask - bid
        data["spread"] = spread
        data["spread_bps"] = (spread / bid) * 10000.0 if bid else None
    lt = data.get("last_trade_time")
    if isinstance(lt, str):
        epoch = _parse_trade_time(lt)
        if epoch:
            data["epoch"] = epoch
    # alias
    if "last_trade" in data and isinstance(data["last_trade"], (int, float)):
        data["last_price"] = data["last_trade"]
    return data


def parse_level1_line_dynamic(line: str) -> Optional[Dict[str, Any]]:
    """Parse a Level1 'Q' message using dynamic fieldnames from the server.

    This requests update fieldnames to infer the exact column order and then maps a
    subset to normalized keys (bid, ask, last_trade, last_trade_time, etc.).
    """
    if not line or not line.startswith("Q,"):
        return None
    try:
        # Lazy import to avoid cycles
        from .iqfeed_fieldmap import request_fieldnames  # type: ignore
        fields = request_fieldnames(max_lines=80)
        names = fields.get("update", []) or []
    except Exception:
        names = []
    if not names:
        return None
    parts = line.strip().split(",")
    # parts[0] == 'Q'; the remainder should align to names
    values = parts[1:]
    n = min(len(values), len(names))
    if n == 0:
        return None
    raw_map: Dict[str, Any] = {names[i]: values[i] for i in range(n)}
    # Normalize a subset of fields
    def _num(x: Any) -> Optional[float]:
        try:
            return float(x)
        except Exception:
            return None
    norm: Dict[str, Any] = {"raw": line}
    # Common mappings observed in IQFeed fieldname lists
    sym = raw_map.get("Symbol")
    if sym:
        norm["symbol"] = sym
    lt = raw_map.get("Most Recent Trade")
    if lt is not None:
        v = _num(lt)
        if v is not None:
            norm["last_trade"] = v
            norm["last_price"] = v
    bid = raw_map.get("Bid")
    ask = raw_map.get("Ask")
    bid_v = _num(bid) if bid is not None else None
    ask_v = _num(ask) if ask is not None else None
    if bid_v is not None:
        norm["bid"] = bid_v
    if ask_v is not None:
        norm["ask"] = ask_v
    if bid_v is not None and ask_v is not None and bid_v > 0 and ask_v > 0:
        mid = (bid_v + ask_v) / 2.0
        norm["mid"] = mid
        spread = ask_v - bid_v
        norm["spread"] = spread
        norm["spread_bps"] = (spread / bid_v) * 10000.0 if bid_v else None
    ltt = raw_map.get("Most Recent Trade Time")
    if ltt:
        norm["last_trade_time"] = ltt
        epoch = _parse_trade_time(ltt)
        if epoch:
            norm["epoch"] = epoch
    return norm


class IQFeedLevel1Stream:
    """Threaded Level1 quote streamer.

    Usage:
        stream = IQFeedLevel1Stream(cfg)
        stream.start(["SPX"])
        msg = stream.get(timeout=2)
    """

    def __init__(self, cfg: IQFeedConfig, on_message: Optional[Callable[[dict[str, Any]], None]] = None,
                 heartbeat_secs: float = 5.0, reconnect_idle_secs: float = 20.0):
        self.cfg = cfg
        self.log = get_logger("iqfeed.stream")
        self.on_message = on_message
        self._sock: Optional[socket.socket] = None
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._queue: "queue.Queue[dict[str, Any]]" = queue.Queue()
        self._watched: set[str] = set()
        self._last_msg_time = time.time()
        self._heartbeat_secs = heartbeat_secs
        self._reconnect_idle_secs = reconnect_idle_secs

    # --- Public API ---
    def start(self, symbols: list[str]):
        if self._thread and self._thread.is_alive():
            self.log.warning("Stream already running")
            return
        self._connect()
        for sym in symbols:
            self.watch(sym)
        self._thread = threading.Thread(target=self._reader_loop, name="iqfeed-l1", daemon=True)
        self._thread.start()

    def stop(self):
        self._stop_event.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=2)
        self._close()

    def watch(self, symbol: str):
        if not self._sock:
            raise RuntimeError("Stream socket not connected. Call start().")
        if symbol in self._watched:
            return
        cmd = f"w{symbol}\r\n"
        self._sock.sendall(cmd.encode("utf-8"))
        self._watched.add(symbol)

    def unwatch(self, symbol: str):
        if not self._sock or symbol not in self._watched:
            return
        # Use 'r' (remove watch) for compatibility
        cmd = f"r{symbol}\r\n"
        try:
            self._sock.sendall(cmd.encode("utf-8"))
        except Exception:  # noqa: BLE001
            pass
        self._watched.discard(symbol)

    def get(self, timeout: Optional[float] = None) -> Optional[dict[str, Any]]:
        try:
            return self._queue.get(timeout=timeout)
        except queue.Empty:
            return None

    # --- Internals ---
    def _connect(self):
        self._sock = socket.create_connection((self.cfg.host, self.cfg.port_level1), timeout=self.cfg.timeout)
        # Set small timeout for responsive shutdown
        self._sock.settimeout(0.5)
        # Basic protocol version negotiation (best effort)
        try:
            self._sock.sendall(b"S,SET PROTOCOL,6.2\r\n")
            self._sock.sendall(b"S,SET CLIENT NAME,ZeroDTE-pipeline\r\n")
            self._sock.sendall(b"S,TIMESTAMPSOFF\r\n")
            # Request update fieldnames and server stats to aid dynamic parsing/diagnostics
            self._sock.sendall(b"S,REQUEST CURRENT UPDATE FIELDNAMES\r\n")
            self._sock.sendall(b"S,REQUEST STATS\r\n")
        except Exception:  # noqa: BLE001
            pass
        # Re-watch existing symbols after reconnect
        if self._watched:
            for sym in list(self._watched):  # ensure iteration stable
                try:
                    self._sock.sendall(f"w{sym}\r\n".encode("utf-8"))
                except Exception:  # noqa: BLE001
                    self.log.debug("Failed to re-watch %s", sym)

    def _close(self):
        if self._sock:
            try:
                self._sock.close()
            except Exception:  # noqa: BLE001
                pass
        self._sock = None

    def _reader_loop(self):  # pragma: no cover - requires live feed
        buf = b""
        while not self._stop_event.is_set():
            try:
                chunk = self._sock.recv(65536) if self._sock else b""
                if not chunk:
                    self._maybe_heartbeat()
                    self._maybe_reconnect()
                    time.sleep(0.05)
                    continue
                # Normalize CR to LF so the loop can split on LF uniformly
                buf += chunk.replace(b"\r", b"\n")
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    text = line.decode("utf-8", errors="replace")
                    # Prefer dynamic parsing when fieldnames are available; fallback to static
                    try:
                        from .iqfeed_client import parse_level1_line_dynamic  # local import safeguard
                    except Exception:
                        parse_level1_line_dynamic = None  # type: ignore
                    parsed = None
                    if parse_level1_line_dynamic is not None:
                        parsed = parse_level1_line_dynamic(text)
                    if not parsed:
                        parsed = parse_level1_line(text)
                    if parsed:
                        self._last_msg_time = time.time()
                        self._queue.put(parsed)
                        if self.on_message:
                            try:
                                self.on_message(parsed)
                            except Exception as exc:  # noqa: BLE001
                                self.log.warning("on_message error: %s", exc)
            except socket.timeout:
                self._maybe_heartbeat()
                self._maybe_reconnect()
                continue
            except Exception as exc:  # noqa: BLE001
                self.log.warning("Reader loop error: %s", exc)
                time.sleep(0.2)
        self.log.info("Reader loop exiting")

    def _maybe_heartbeat(self):  # pragma: no cover - timing dependent
        if not self._sock:
            return
        now = time.time()
        if (now - self._last_msg_time) >= self._heartbeat_secs:
            try:
                self._sock.sendall(b"S,SERVER CONNECTED\r\n")  # simple ping
            except Exception:  # noqa: BLE001
                pass

    def _maybe_reconnect(self):  # pragma: no cover - timing dependent
        now = time.time()
        if (now - self._last_msg_time) >= self._reconnect_idle_secs:
            self.log.warning("Idle %.1fs >= %.1fs threshold; reconnecting", (now - self._last_msg_time), self._reconnect_idle_secs)
            try:
                self._close()
            except Exception:  # noqa: BLE001
                pass
            time.sleep(0.25)
            try:
                self._connect()
                self._last_msg_time = time.time()
            except Exception as exc:  # noqa: BLE001
                self.log.error("Reconnect failed: %s", exc)
                # leave for next cycle


def should_reconnect(last_time: float, now: float, threshold: float) -> bool:
    """Pure helper to determine if reconnect should trigger (for unit testing)."""
    return (now - last_time) >= threshold


__all__ = [
    "IQFeedClient",
    "IQFeedConfig",
    "IQFeedLevel1Stream",
    "parse_level1_line",
    "should_reconnect",
]
