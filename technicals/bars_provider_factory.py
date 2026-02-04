from __future__ import annotations

import os
from typing import Optional, Protocol

import pandas as pd


# Keep imports local so missing deps don’t crash module import time.
# Providers must implement get_bars(symbol, timeframe, lookback_bars) -> DataFrame


class BarsProvider(Protocol):
    def get_bars(self, symbol: str, timeframe: str, lookback_bars: int) -> pd.DataFrame:  # pragma: no cover
        ...


def build_bars_provider_from_env() -> Optional[BarsProvider]:
    """Select bars provider by env.

    Default: polygon (if POLYGON_API_KEY or MASSIVE_API_KEY is set)

    Env:
      - TNT_BARS_PROVIDER: "polygon" (default), future: "redis", "yfinance", etc.
      - POLYGON_API_KEY or MASSIVE_API_KEY
      - POLYGON_BASE_URL or MASSIVE_BASE_URL (optional)
    """

    provider = (os.getenv("TNT_BARS_PROVIDER") or "polygon").strip().lower()
    cache = (os.getenv("TNT_BARS_CACHE") or "redis").strip().lower()
    cache_enabled = cache not in {"0", "false", "off", "none", "disable", "disabled"}

    if provider == "polygon":
        from technicals.polygon_bars_provider import PolygonBarsProvider

        try:
            base = PolygonBarsProvider.from_env()
        except Exception:
            return None

        if cache_enabled:
            from technicals.cached_bars_provider import CachedBarsProvider

            return CachedBarsProvider(inner=base, provider_id="polygon")

        return base

    # Future extension points:
    # if provider == "redis":
    #     from technicals.redis_bars_provider import RedisBarsProvider
    #     return RedisBarsProvider.from_env()
    #
    # if provider == "yfinance":
    #     from technicals.yfinance_bars_provider import YFinanceBarsProvider
    #     return YFinanceBarsProvider()

    return None
