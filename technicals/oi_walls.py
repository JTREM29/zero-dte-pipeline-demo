from __future__ import annotations

from dataclasses import dataclass
from typing import Optional
import os


@dataclass
class OIWalls:
    symbol: str
    call_wall: Optional[float]
    put_wall: Optional[float]
    ts_et: Optional[str] = None
    px: Optional[float] = None
    call_oi: Optional[float] = None
    put_oi: Optional[float] = None
    expiry: Optional[str] = None


def _redis_from_env():
    host = (os.getenv("TNT_REDIS_HOST") or os.getenv("REDIS_HOST") or "127.0.0.1").strip()
    port_s = (os.getenv("TNT_REDIS_PORT") or os.getenv("REDIS_PORT") or "6379").strip()
    db_s = (os.getenv("TNT_REDIS_DB") or os.getenv("REDIS_DB") or "0").strip()

    try:
        port = int(port_s)
    except Exception:
        port = 6379
    try:
        db = int(db_s)
    except Exception:
        db = 0

    user = os.getenv("TNT_REDIS_USERNAME") or os.getenv("REDIS_USERNAME")
    pwd = os.getenv("TNT_REDIS_PASSWORD") or os.getenv("REDIS_PASSWORD")

    try:
        import redis  # type: ignore

        return redis.Redis(
            host=host,
            port=port,
            db=db,
            username=(user.strip() if isinstance(user, str) and user.strip() else None),
            password=(pwd.strip() if isinstance(pwd, str) and pwd.strip() else None),
            socket_timeout=0.35,
            socket_connect_timeout=0.35,
            decode_responses=True,
        )
    except Exception:
        return None


def get_oi_walls(symbol: str) -> Optional[OIWalls]:
    """Read precomputed OI walls from Redis.

    Returns None if Redis is unavailable or the key is missing.

    Key: oi:walls:{SYMBOL}
    Type: Redis hash
    """

    sym = (symbol or "").strip().upper()
    if not sym:
        return None

    r = _redis_from_env()
    if r is None:
        return None

    key = f"oi:walls:{sym}"
    try:
        m = r.hgetall(key)
    except Exception:
        return None

    if not m:
        return None

    def fnum(x):
        try:
            if x is None:
                return None
            s = str(x).strip()
            if not s:
                return None
            return float(s)
        except Exception:
            return None

    return OIWalls(
        symbol=sym,
        call_wall=fnum(m.get("call_wall")),
        put_wall=fnum(m.get("put_wall")),
        ts_et=(str(m.get("ts_et") or "").strip() or None),
        px=fnum(m.get("px")),
        call_oi=fnum(m.get("call_oi")),
        put_oi=fnum(m.get("put_oi")),
        expiry=(str(m.get("expiry") or "").strip() or None),
    )


def set_oi_walls(
    symbol: str,
    *,
    call_wall: float | None,
    put_wall: float | None,
    ts_et: str | None = None,
    px: float | None = None,
    call_oi: float | None = None,
    put_oi: float | None = None,
    expiry: str | None = None,
    ttl_s: int = 900,
) -> bool:
    """Publish OI walls to Redis as a small hash.

    Key: oi:walls:{SYMBOL}
    Type: Redis hash
    """

    sym = (symbol or "").strip().upper()
    if not sym:
        return False

    if call_wall is None and put_wall is None:
        return False

    r = _redis_from_env()
    if r is None:
        return False

    def _s(x) -> str | None:
        if x is None:
            return None
        try:
            v = str(x).strip()
            return v or None
        except Exception:
            return None

    def _f(x) -> str | None:
        if x is None:
            return None
        try:
            return str(float(x))
        except Exception:
            return None

    mapping: dict[str, str] = {}
    for k, v in (
        ("call_wall", _f(call_wall)),
        ("put_wall", _f(put_wall)),
        ("ts_et", _s(ts_et)),
        ("px", _f(px)),
        ("call_oi", _f(call_oi)),
        ("put_oi", _f(put_oi)),
        ("expiry", _s(expiry)),
    ):
        if v is not None:
            mapping[k] = v

    if not mapping:
        return False

    key = f"oi:walls:{sym}"
    try:
        r.hset(key, mapping=mapping)
        r.expire(key, max(30, int(ttl_s)))
        return True
    except Exception:
        return False
