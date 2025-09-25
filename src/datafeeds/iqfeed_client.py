"""IQFeed client placeholder.

Real implementation would manage TCP socket login, request chains, and streaming updates.
"""
from __future__ import annotations
from dataclasses import dataclass
from typing import Any, Optional
import os
import socket
import time


@dataclass
class IQFeedConfig:
    username: str | None
    password: str | None
    host: str = "127.0.0.1"
    port_level1: int = 5009
    port_admin: int = 5009
    timeout: float = 2.0

    @classmethod
    def from_env(cls) -> "IQFeedConfig":
        return cls(
            username=os.getenv("IQFEED_USERNAME"),
            password=os.getenv("IQFEED_PASSWORD"),
            host=os.getenv("IQFEED_HOST", "127.0.0.1"),
            port_level1=int(os.getenv("IQFEED_PORT_LEVEL1", "5009")),
            port_admin=int(os.getenv("IQFEED_PORT_ADMIN", "5009")),
            timeout=float(os.getenv("IQFEED_TIMEOUT", "2.0")),
        )


class IQFeedClient:
    def __init__(self, cfg: IQFeedConfig):
        self._cfg = cfg
        self.log = get_logger("iqfeed")  # type: ignore[assignment]
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
            # Negotiate protocol (optional)
            self._send_and_recv("S,SET PROTOCOL,6.2\n")
            res = self._send_and_recv(f"w{symbol}\n")
            self._send_and_recv(f"u{symbol}\n")  # unwatch
            return {"symbol": symbol, "raw": res}
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
