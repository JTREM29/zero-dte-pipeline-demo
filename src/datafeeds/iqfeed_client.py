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
    port_admin: int = 5009
    port_lookup: int = 5009  # separate lookup/derivative requests if needed
    timeout: float = 2.0

    @classmethod
    def from_env(cls) -> "IQFeedConfig":
        return cls(
            username=os.getenv("IQFEED_USERNAME"),
            password=os.getenv("IQFEED_PASSWORD"),
            host=os.getenv("IQFEED_HOST", "127.0.0.1"),
            port_level1=int(os.getenv("IQFEED_PORT_LEVEL1", "5009")),
            port_admin=int(os.getenv("IQFEED_PORT_ADMIN", "5009")),
            port_lookup=int(os.getenv("IQFEED_PORT_LOOKUP", os.getenv("IQFEED_PORT_LEVEL1", "5009"))),
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
            # Negotiate protocol (optional) then watch once
            self._send_and_recv("S,SET PROTOCOL,6.2\n")
            res = self._send_and_recv(f"w{symbol}\n")
            self._send_and_recv(f"u{symbol}\n")  # unwatch immediately
            payload: dict[str, Any] = {"symbol": symbol, "raw": res}
            # Attempt to parse first Q line for structured fields
            for line in res.splitlines():
                if line.startswith("Q,"):
                    from .iqfeed_client import parse_level1_line  # local import to avoid circular
                    parsed = parse_level1_line(line)
                    if parsed:
                        payload.update({k: v for k, v in parsed.items() if k not in {"raw"}})
                        # Provide canonical last_price alias
                        lp = parsed.get("last_trade")
                        if isinstance(lp, (int, float)):
                            payload["last_price"] = lp
                    break
            return payload
        except Exception as e:  # noqa: BLE001
            return None

    def ping(self) -> bool:
        try:
            self._send_and_recv("S,SERVER CONNECTED\n")
            return True
        except Exception as e:  # noqa: BLE001
            return False

    def fetch_demo_chain(self, root: str) -> list[dict[str, Any]]:
        # Placeholder static symbols
        return [
            {"symbol": f"{root}250925C00000000", "type": "call", "_demo": True},
            {"symbol": f"{root}250925P00000000", "type": "put", "_demo": True},
        ]


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
        cmd = f"w{symbol}\n"
        self._sock.sendall(cmd.encode("utf-8"))
        self._watched.add(symbol)

    def unwatch(self, symbol: str):
        if not self._sock or symbol not in self._watched:
            return
        cmd = f"u{symbol}\n"
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
            self._sock.sendall(b"S,SET PROTOCOL,6.2\n")
        except Exception:  # noqa: BLE001
            pass
        # Re-watch existing symbols after reconnect
        if self._watched:
            for sym in list(self._watched):  # ensure iteration stable
                try:
                    self._sock.sendall(f"w{sym}\n".encode("utf-8"))
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
                buf += chunk
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    text = line.decode("utf-8", errors="replace")
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
                self._sock.sendall(b"S,SERVER CONNECTED\n")  # simple ping
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
