"""Polygon.io client abstraction.

Contains minimal REST helper for previous aggregate close (SPX) and snapshot placeholder.
"""
from __future__ import annotations
from dataclasses import dataclass
from typing import Any, Optional
import os
import requests

from src.utils.logging_setup import get_logger


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
        self.log = get_logger("polygon")
        self.base = "https://api.polygon.io"
        self.api_key = cfg.api_key

    def fetch_underlying_snapshot(self, symbol: str) -> dict[str, Any]:
        # Placeholder snapshot; real endpoint would call /v2/snapshot...
        return {"symbol": symbol, "lastPrice": 0.0, "source": "polygon", "_demo": True}

    def last_trade_spx(self) -> Optional[dict[str, Any]]:
        """Fetch previous aggregate bar for SPX index (uses /v2/aggs/ticker/SPX/prev)."""
        if not self.api_key:
            self.log.warning("Polygon API key missing.")
            return None
        url = f"{self.base}/v2/aggs/ticker/SPX/prev"
        params = {"adjusted": "true", "apiKey": self.api_key}
        try:
            r = requests.get(url, params=params, timeout=10)
        except Exception as exc:  # noqa: BLE001
            self.log.error("Polygon request failed: %s", exc)
            return None
        if r.status_code != 200:
            self.log.error("Polygon error %s: %s", r.status_code, r.text[:300])
            return None
        return r.json()
