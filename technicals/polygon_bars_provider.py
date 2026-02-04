from __future__ import annotations

from dataclasses import dataclass
import os
import time
from typing import Optional

import pandas as pd
import requests


_TF_MAP: dict[str, tuple[int, str]] = {
    "1m": (1, "minute"),
    "3m": (3, "minute"),
    "5m": (5, "minute"),
    "15m": (15, "minute"),
    "30m": (30, "minute"),
    "1h": (1, "hour"),
    "2h": (2, "hour"),
    "4h": (4, "hour"),
    "1d": (1, "day"),
}


@dataclass
class PolygonBarsProvider:
    """Fetch OHLC bars via Polygon-style aggregates endpoint.

    Works with a Polygon-compatible gateway when pointed at its base URL.

    Expects env:
      - POLYGON_API_KEY (preferred) or MASSIVE_API_KEY
      - POLYGON_BASE_URL (optional; default https://api.polygon.io)
      - MASSIVE_BASE_URL (optional)

    Returns a DataFrame with columns: ts, open, high, low, close, volume
    """

    api_key: str
    base_url: str = "https://api.polygon.io"
    session: Optional[requests.Session] = None

    @classmethod
    def from_env(cls) -> "PolygonBarsProvider":
        api_key = (os.getenv("POLYGON_API_KEY") or os.getenv("MASSIVE_API_KEY") or "").strip()
        if not api_key:
            raise RuntimeError("Missing POLYGON_API_KEY (or MASSIVE_API_KEY)")

        base_url = (
            os.getenv("POLYGON_BASE_URL")
            or os.getenv("MASSIVE_BASE_URL")
            or "https://api.polygon.io"
        )
        base_url = (base_url or "https://api.polygon.io").strip().rstrip("/")
        return cls(api_key=api_key, base_url=base_url)

    def get_bars(self, symbol: str, timeframe: str, lookback_bars: int) -> pd.DataFrame:
        sym = (symbol or "").strip().upper()
        tf = (timeframe or "").strip().lower()
        if not sym:
            raise ValueError("Missing symbol")
        if tf not in _TF_MAP:
            raise ValueError(f"Unsupported timeframe: {tf} (supported: {sorted(_TF_MAP.keys())})")

        mult, span = _TF_MAP[tf]

        # Overshoot so RSI + pivot logic has enough history (still deterministic).
        bars = max(int(lookback_bars), 220)
        sec_per = {"minute": 60, "hour": 3600, "day": 86400}[span]
        window_s = int(bars * int(mult) * int(sec_per))

        now_ms = int(time.time() * 1000)
        from_ms = now_ms - int(window_s * 1000 * 1.2)

        url = f"{self.base_url}/v2/aggs/ticker/{sym}/range/{int(mult)}/{span}/{from_ms}/{now_ms}"
        params = {
            "adjusted": "true",
            "sort": "asc",
            "limit": 50000,
            "apiKey": self.api_key,
        }

        s = self.session or requests.Session()
        r = s.get(url, params=params, timeout=15)
        r.raise_for_status()
        payload = r.json()

        results = payload.get("results") or []
        if not results:
            return pd.DataFrame(columns=["ts", "open", "high", "low", "close", "volume"])

        df = pd.DataFrame(results)
        df = df.rename(columns={"t": "ts", "o": "open", "h": "high", "l": "low", "c": "close", "v": "volume"})
        # Polygon aggregate timestamps are epoch ms (UTC). Convert to market clock (ET)
        # so RTH filtering is deterministic.
        df["ts"] = pd.to_datetime(df["ts"], unit="ms", utc=True).dt.tz_convert("America/New_York")

        for col in ("open", "high", "low", "close", "volume"):
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], errors="coerce")

        df = df.dropna(subset=["ts", "high", "low", "close"]).reset_index(drop=True)
        return df.tail(int(lookback_bars)).reset_index(drop=True)
