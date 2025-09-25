"""Polygon.io client abstraction (placeholder).

Add real REST/WebSocket calls later. Keeps interface small so tests can mock it.
"""
from __future__ import annotations
from dataclasses import dataclass
from typing import Any
import os


@dataclass
class PolygonConfig:
    api_key: str

    @classmethod
    def from_env(cls) -> "PolygonConfig":
        key = os.getenv("POLYGON_API_KEY", "")
        if not key:
            raise RuntimeError("POLYGON_API_KEY not set in environment")
        return cls(api_key=key)


class PolygonClient:
    def __init__(self, cfg: PolygonConfig):
        self._cfg = cfg

    def fetch_underlying_snapshot(self, symbol: str) -> dict[str, Any]:
        # Placeholder; replace with actual HTTP call
        return {"symbol": symbol, "lastPrice": 0.0, "source": "polygon", "_demo": True}
