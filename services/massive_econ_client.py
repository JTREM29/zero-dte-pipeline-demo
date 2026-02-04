from __future__ import annotations

import os
import time
from dataclasses import dataclass
from typing import Any, Dict, Optional

import requests


@dataclass
class MassiveEconClient:
    api_key: str
    base_url: str = "https://api.massive.com"
    timeout_s: int = 15

    def _get(self, path: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        if not (self.api_key or "").strip():
            raise RuntimeError("MASSIVE_API_KEY missing")
        url = self.base_url.rstrip("/") + path
        q = dict(params or {})
        q["apiKey"] = (self.api_key or "").strip()
        r = requests.get(url, params=q, timeout=int(self.timeout_s))
        r.raise_for_status()
        data = r.json()
        if not isinstance(data, dict):
            raise RuntimeError("Massive response was not a JSON object")
        return data

    def get_treasury_yields(self, limit: int = 1, sort: str = "date.desc") -> Dict[str, Any]:
        return self._get("/fed/v1/treasury-yields", {"limit": int(limit), "sort": str(sort)})

    def get_inflation(self, limit: int = 1, sort: str = "date.desc") -> Dict[str, Any]:
        return self._get("/fed/v1/inflation", {"limit": int(limit), "sort": str(sort)})

    def get_inflation_expectations(self, limit: int = 1, sort: str = "date.desc") -> Dict[str, Any]:
        return self._get("/fed/v1/inflation-expectations", {"limit": int(limit), "sort": str(sort)})

    def get_labor_market(self, limit: int = 1, sort: str = "date.desc") -> Dict[str, Any]:
        return self._get("/fed/v1/labor-market", {"limit": int(limit), "sort": str(sort)})


def build_massive_client_from_env() -> MassiveEconClient:
    return MassiveEconClient(
        api_key=(os.getenv("MASSIVE_API_KEY", "") or "").strip(),
        base_url=(os.getenv("MASSIVE_BASE_URL", "https://api.massive.com") or "https://api.massive.com").strip(),
    )


def normalize_massive_payload(series: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    """Normalize Massive series responses to a consistent record for Redis + embeds.

    Uses the newest item in results (assuming sort=date.desc).
    """

    results = payload.get("results") or []
    latest = results[0] if isinstance(results, list) and results else {}
    now_utc = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    return {
        "source": "massive",
        "series": str(series or ""),
        "asof_date": (latest or {}).get("date"),
        "values": latest if isinstance(latest, dict) else {},
        "count": payload.get("count"),
        "request_id": payload.get("request_id"),
        "status": payload.get("status"),
        "updated_utc": now_utc,
    }
