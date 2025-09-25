"""Polygon.io client abstraction.

Contains minimal REST helper for previous aggregate close (SPX) and snapshot placeholder.
"""
from __future__ import annotations
from dataclasses import dataclass
from typing import Any, Optional
import os
import requests
import time
from functools import lru_cache
import pandas as pd

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
        self._cache: dict[str, tuple[float, dict[str, Any]]] = {}
        self.cache_ttl = 30.0  # seconds

    def _cached(self, key: str) -> Optional[dict[str, Any]]:
        now = time.time()
        entry = self._cache.get(key)
        if not entry:
            return None
        ts, payload = entry
        if now - ts > self.cache_ttl:
            self._cache.pop(key, None)
            return None
        return payload

    def _store_cache(self, key: str, payload: dict[str, Any]) -> None:
        self._cache[key] = (time.time(), payload)

    def fetch_underlying_snapshot(self, symbol: str) -> dict[str, Any]:
        # Placeholder snapshot; real endpoint would call /v2/snapshot...
        return {"symbol": symbol, "lastPrice": 0.0, "source": "polygon", "_demo": True}

    def _request(self, url: str, params: dict[str, Any], retries: int = 3, backoff: float = 0.5) -> Optional[dict[str, Any]]:
        for attempt in range(1, retries + 1):
            try:
                r = requests.get(url, params=params, timeout=10)
            except Exception as exc:  # noqa: BLE001
                self.log.warning("Attempt %s failed: %s", attempt, exc)
                time.sleep(backoff * attempt)
                continue
            if r.status_code == 200:
                try:
                    return r.json()
                except Exception as exc:  # noqa: BLE001
                    self.log.error("JSON decode error: %s", exc)
                    return None
            else:
                self.log.warning("Non-200 (%s) attempt %s: %s", r.status_code, attempt, r.text[:200])
                time.sleep(backoff * attempt)
        return None

    def last_trade_spx(self, use_cache: bool = True) -> Optional[dict[str, Any]]:
        """Fetch previous aggregate bar for SPX index (uses /v2/aggs/ticker/SPX/prev)."""
        if not self.api_key:
            self.log.warning("Polygon API key missing.")
            return None
        cache_key = "spx_prev"
        if use_cache and (cached := self._cached(cache_key)):
            return cached
        url = f"{self.base}/v2/aggs/ticker/SPX/prev"
        params = {"adjusted": "true", "apiKey": self.api_key}
        data = self._request(url, params)
        if data:
            self._store_cache(cache_key, data)
        return data

    @staticmethod
    def normalize_prev_agg(payload: dict[str, Any]) -> Optional[pd.DataFrame]:
        # Expected structure: { results: [ { "o":..., "h":..., "l":..., "c":..., "v":..., "t": ... } ], ... }
        results = payload.get("results") if isinstance(payload, dict) else None
        if not results or not isinstance(results, list):
            return None
        df = pd.DataFrame(results)
        # Rename columns to readable names
        rename_map = {"o": "open", "h": "high", "l": "low", "c": "close", "v": "volume", "t": "timestamp"}
        df = df.rename(columns=rename_map)
        return df
