from __future__ import annotations

import json
import os
import socket
import time
from dataclasses import dataclass
from typing import Any, TYPE_CHECKING

if TYPE_CHECKING:
    import redis as redis_lib

    RedisClient = redis_lib.Redis
else:
    RedisClient = Any


@dataclass(frozen=True)
class FeedHeartbeat:
    ts: int
    pid: int
    host: str
    last_error: str | None = None

    def age_s(self, *, now: int | None = None) -> int:
        n = int(now if now is not None else time.time())
        return max(0, n - int(self.ts))


_REDIS: RedisClient | None = None


def _redis_client() -> RedisClient:
    global _REDIS
    if _REDIS is not None:
        return _REDIS

    try:
        import redis as redis_lib
    except ModuleNotFoundError as e:
        raise RuntimeError(
            "redis-py is not installed (required for feed heartbeats). "
            "Install `redis` or run with the project virtualenv."
        ) from e

    host = os.getenv("TNT_REDIS_HOST", "127.0.0.1")
    port = int(os.getenv("TNT_REDIS_PORT", "6379"))
    db = int(os.getenv("TNT_REDIS_DB", "0"))
    _REDIS = redis_lib.Redis(host=host, port=port, db=db, decode_responses=True)
    return _REDIS


def write_feed_heartbeat(
    feed: str,
    *,
    last_error: str | None = None,
    ttl_sec: int = 10 * 60,
    extra: dict[str, Any] | None = None,
) -> FeedHeartbeat | None:
    """Write `hb:{feed}` heartbeat to Redis.

    Payload includes ts (epoch seconds), pid, host, and optional last_error.

    Returns the parsed heartbeat on success; returns None on failure.
    """

    name = str(feed or "").strip().lower()
    if not name:
        return None

    ts = int(time.time())
    payload: dict[str, Any] = {
        "ts": ts,
        "pid": int(os.getpid()),
        "host": socket.gethostname(),
    }
    if last_error and str(last_error).strip():
        payload["last_error"] = str(last_error).strip()[:500]
    if extra:
        try:
            payload.update({str(k): v for k, v in extra.items()})
        except Exception:
            pass

    try:
        r = _redis_client()
        key = f"hb:{name}"
        r.set(key, json.dumps(payload, separators=(",", ":"), ensure_ascii=False))
        if ttl_sec and int(ttl_sec) > 0:
            r.expire(key, int(ttl_sec))
        return FeedHeartbeat(
            ts=ts,
            pid=int(payload["pid"]),
            host=str(payload["host"]),
            last_error=str(payload.get("last_error") or "").strip() or None,
        )
    except Exception:
        return None


def read_feed_heartbeat(feed: str) -> FeedHeartbeat | None:
    name = str(feed or "").strip().lower()
    if not name:
        return None

    try:
        raw = _redis_client().get(f"hb:{name}")
    except Exception:
        return None

    if not raw:
        return None

    try:
        js = json.loads(raw)
        if not isinstance(js, dict):
            return None
        ts = int(js.get("ts") or 0)
        pid = int(js.get("pid") or 0)
        host = str(js.get("host") or "").strip()
        last_error = str(js.get("last_error") or "").strip() or None
        if ts <= 0 or pid <= 0 or not host:
            return None
        return FeedHeartbeat(ts=ts, pid=pid, host=host, last_error=last_error)
    except Exception:
        return None


def required_feeds_from_env(default: str = "polygon_rest,futures") -> set[str]:
    raw = (os.getenv("TNT_FEEDS_REQUIRED") or default).strip()
    feeds = {s.strip().lower() for s in raw.split(",") if s.strip()}
    return feeds


def stale_thresholds_from_env() -> dict[str, int]:
    """Per-feed staleness thresholds in seconds (configurable via env)."""

    def _ival(name: str, default: int) -> int:
        try:
            v = int(os.getenv(name, str(default)) or str(default))
        except Exception:
            v = default
        return max(1, min(24 * 3600, v))

    return {
        "polygon_rest": _ival("TNT_FEED_STALE_POLYGON_REST_SEC", 180),
        "polygon_ws": _ival("TNT_FEED_STALE_POLYGON_WS_SEC", 60),
        "futures": _ival("TNT_FEED_STALE_FUTURES_SEC", 60),
        "news": _ival("TNT_FEED_STALE_NEWS_SEC", 120),
    }


def is_stale(hb: FeedHeartbeat | None, *, threshold_s: int) -> bool:
    if hb is None:
        return True
    try:
        return hb.age_s() > int(threshold_s)
    except Exception:
        return True


def _fmt_age(age_s: int) -> str:
    a = max(0, int(age_s))
    if a < 90:
        return f"{a}s"
    if a < 3600:
        return f"{a // 60}m"
    return f"{a // 3600}h{(a % 3600) // 60:02d}m"


def summarize_feeds(
    *,
    feeds: list[str] | None = None,
    required: set[str] | None = None,
    thresholds: dict[str, int] | None = None,
) -> tuple[str, dict[str, dict[str, Any]]]:
    """Return a compact single-line feed summary + per-feed details.

    Summary example:
    Feeds: polygon_rest OK (8s) | polygon_ws OK (12s) | futures OK (9s) | news OK (45s)
    """

    feeds = feeds or ["polygon_rest", "polygon_ws", "futures", "news"]
    required = required or required_feeds_from_env()
    thresholds = thresholds or stale_thresholds_from_env()

    details: dict[str, dict[str, Any]] = {}
    parts: list[str] = []
    now = int(time.time())

    for name in feeds:
        f = str(name or "").strip().lower()
        if not f:
            continue
        hb = read_feed_heartbeat(f)
        th = int(thresholds.get(f, 120))
        req = f in required
        age = hb.age_s(now=now) if hb else 10**9

        stale = age > th
        status = "STALE" if stale else "OK"
        if hb is None:
            status = "MISSING"

        tag = f"{f}"
        if req:
            tag = f"{tag}*"
        parts.append(f"{tag} {status} ({_fmt_age(age)})")

        details[f] = {
            "required": req,
            "threshold_s": th,
            "status": status,
            "age_s": int(age),
            "ts": getattr(hb, "ts", None),
            "pid": getattr(hb, "pid", None),
            "host": getattr(hb, "host", None),
            "last_error": getattr(hb, "last_error", None),
        }

    return "Feeds: " + " | ".join(parts), details
