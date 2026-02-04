from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Any, Optional, Protocol

import pandas as pd


class _RedisLike(Protocol):
    def get(self, key: str) -> Any:  # pragma: no cover
        ...

    def setex(self, key: str, ttl: int, value: str) -> Any:  # pragma: no cover
        ...


def _ttl_for_timeframe(tf: str) -> int:
    t = (tf or "").strip().lower()
    if t in {"1m", "3m", "5m"}:
        return 15
    if t == "15m":
        return 30
    if t == "30m":
        return 45
    if t in {"1h"}:
        return 60
    if t in {"2h"}:
        return 90
    if t in {"4h", "1d"}:
        return 120
    return 30


def _redis_from_env() -> Optional[_RedisLike]:
    """Best-effort Redis client built from env.

    Lazy import + short timeouts so this can never hang the bot.
    """

    host = (os.getenv("TNT_REDIS_HOST") or "127.0.0.1").strip()
    port_s = (os.getenv("TNT_REDIS_PORT") or "6379").strip()
    db_s = (os.getenv("TNT_REDIS_DB") or "0").strip()

    try:
        port = int(port_s)
    except Exception:
        port = 6379
    try:
        db = int(db_s)
    except Exception:
        db = 0

    password = os.getenv("TNT_REDIS_PASSWORD")
    username = os.getenv("TNT_REDIS_USERNAME")

    try:
        import redis  # type: ignore

        return redis.Redis(
            host=host,
            port=port,
            db=db,
            username=(username.strip() if isinstance(username, str) and username.strip() else None),
            password=(password.strip() if isinstance(password, str) and password.strip() else None),
            decode_responses=True,
            socket_connect_timeout=0.35,
            socket_timeout=0.75,
        )
    except Exception:
        return None


def _df_to_payload(df: pd.DataFrame) -> str:
    # Keep it simple and portable: JSON records + ISO timestamps.
    # Keep payload size sane by storing only the necessary columns.
    keep = ["ts", "open", "high", "low", "close", "volume"]
    dfx = df.copy()
    present = [c for c in keep if c in dfx.columns]
    if present:
        dfx = dfx[present]
    if "ts" in dfx.columns:
        dfx = dfx.dropna(subset=["ts"]).reset_index(drop=True)
    if "close" in dfx.columns:
        dfx = dfx.dropna(subset=["close"]).reset_index(drop=True)
    rows = dfx.to_dict(orient="records")
    return json.dumps(rows, separators=(",", ":"), default=str)


def _payload_to_df(payload: str) -> pd.DataFrame:
    try:
        rows = json.loads(payload)
    except Exception:
        return pd.DataFrame()
    if not isinstance(rows, list):
        return pd.DataFrame()
    df = pd.DataFrame(rows)
    if "ts" in df.columns:
        df["ts"] = pd.to_datetime(df["ts"], utc=False, errors="coerce")
    return df


@dataclass
class CachedBarsProvider:
    """Redis-backed cooldown cache for OHLC bars.

    Wraps any provider that implements get_bars(symbol, timeframe, lookback_bars) -> DataFrame.

    Cache key shape:
      bars:{provider}:{symbol}:{tf}:{lookback}

    Behavior is deterministic and safe:
      - If Redis is unavailable, falls back to direct fetch.
      - No import-time Redis connections.
    """

    inner: Any
    provider_id: str
    redis_client: Optional[_RedisLike] = None
    enabled: bool = True

    # Diagnostics for callers (optional)
    last_cache_hit: bool = False
    last_ttl_s: int = 0

    def _redis(self) -> Optional[_RedisLike]:
        if not self.enabled:
            return None
        if self.redis_client is not None:
            return self.redis_client
        self.redis_client = _redis_from_env()
        return self.redis_client

    def _key(self, symbol: str, timeframe: str, lookback_bars: int) -> str:
        sym = (symbol or "").strip().upper()
        tf = (timeframe or "").strip().lower()
        n = int(lookback_bars)
        return f"bars:{self.provider_id}:{sym}:{tf}:{n}"

    def get_bars(self, symbol: str, timeframe: str, lookback_bars: int) -> pd.DataFrame:
        r = self._redis()
        ttl = _ttl_for_timeframe(timeframe)
        self.last_ttl_s = int(ttl)

        if r is not None:
            key = self._key(symbol, timeframe, lookback_bars)
            try:
                raw = r.get(key)
            except Exception:
                raw = None
            if isinstance(raw, str) and raw:
                self.last_cache_hit = True
                df = _payload_to_df(raw)
                if df is not None and len(df) > 0:
                    return df

        self.last_cache_hit = False
        df = self.inner.get_bars(symbol, timeframe, lookback_bars)

        if r is not None:
            key = self._key(symbol, timeframe, lookback_bars)
            try:
                payload = _df_to_payload(df)
                if payload:
                    r.setex(key, int(ttl), payload)
            except Exception:
                pass

        return df
