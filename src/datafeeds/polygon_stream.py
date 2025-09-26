"""Polygon WebSocket streaming skeleton (placeholder).

Does not open a real connection yet—intended shape only.
"""
from __future__ import annotations
from dataclasses import dataclass
from typing import Callable, Any, Optional

try:
    import websocket  # type: ignore
except Exception:  # pragma: no cover
    websocket = None  # type: ignore

from src.utils.logging_setup import get_logger


@dataclass
class PolygonStreamConfig:
    api_key: str
    channel: str = "XA"  # example channel
    url: str = "wss://socket.polygon.io/stocks"


class PolygonStream:
    def __init__(self, cfg: PolygonStreamConfig, on_message: Callable[[dict[str, Any]], None]):
        self.cfg = cfg
        self.on_message = on_message
        self.log = get_logger("polygon.stream")
        self.ws = None

    def connect(self):  # pragma: no cover - placeholder
        if websocket is None:
            self.log.warning("websocket-client not installed")
            return
        self.log.info("(placeholder) Would connect to %s", self.cfg.url)

    def close(self):  # pragma: no cover - placeholder
        if self.ws:
            try:
                self.ws.close()
            except Exception:  # noqa: BLE001
                pass
