"""Proof-friendly tail of Redis results.

Prints the last N entries of the results key (default: TNT_REDIS_RESULTS / tnt:results).
Designed for screenshots: includes UTC timestamp, key/type/len, and truncated payload.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone

import redis


def _env_int(name: str, default: int) -> int:
    raw = (os.getenv(name, "") or "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except Exception:
        return default


def _redis_client() -> redis.Redis:
    host = (os.getenv("TNT_REDIS_HOST", "127.0.0.1") or "127.0.0.1").strip()
    port = _env_int("TNT_REDIS_PORT", 6379)
    db = _env_int("TNT_REDIS_DB", 0)
    # Keep proof tooling snappy: avoid hanging indefinitely when Redis is down.
    timeout_s = float(os.getenv("TNT_REDIS_TIMEOUT_S", "0.5") or "0.5")
    timeout_s = max(0.1, min(timeout_s, 5.0))
    return redis.Redis(
        host=host,
        port=port,
        db=db,
        decode_responses=False,
        socket_connect_timeout=timeout_s,
        socket_timeout=timeout_s,
    )


def _safe_decode(value: bytes | str | None) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        try:
            return value.decode("utf-8")
        except Exception:
            return value.decode("utf-8", errors="replace")
    return str(value)


def main() -> int:
    n = int(os.getenv("TNT_RESULTS_TAIL_N", "8"))
    max_chars = int(os.getenv("TNT_RESULTS_TAIL_MAX_CHARS", "600"))

    results_key = (os.getenv("TNT_REDIS_RESULTS", "tnt:results") or "tnt:results").strip()
    now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

    try:
        r = _redis_client()
        key_type = _safe_decode(r.type(results_key)) or "none"
    except Exception as exc:
        print(f"(redis error: {type(exc).__name__}: {exc})")
        return 0

    print(f"utc={now} key={results_key} type={key_type}")

    if key_type == "list":
        length = int(r.llen(results_key))
        print(f"len={length} tail_n={n}")
        if length == 0:
            return 0
        start = max(0, length - n)
        items = r.lrange(results_key, start, -1)
        for idx, raw in enumerate(items, start=start):
            s = _safe_decode(raw).replace("\r", " ").replace("\n", " ")
            if len(s) > max_chars:
                s = s[:max_chars] + "…"
            print(f"[{idx}] {s}")
        return 0

    if key_type == "stream":
        length = int(r.xlen(results_key))
        print(f"len={length} tail_n={n}")
        if length == 0:
            return 0
        entries = r.xrevrange(results_key, count=n)
        for entry_id, fields in entries:
            field_str = { _safe_decode(k): _safe_decode(v) for k, v in (fields or {}).items() }
            s = str(field_str).replace("\r", " ").replace("\n", " ")
            if len(s) > max_chars:
                s = s[:max_chars] + "…"
            print(f"[{_safe_decode(entry_id)}] {s}")
        return 0

    if key_type in {"none", ""}:
        print("(missing)")
        return 0

    # Other types: show cardinality-ish info only.
    try:
        if key_type == "zset":
            print(f"zcard={int(r.zcard(results_key))}")
        elif key_type == "set":
            print(f"scard={int(r.scard(results_key))}")
        elif key_type == "hash":
            print(f"hlen={int(r.hlen(results_key))}")
        else:
            print("(tail unsupported for this type)")
    except Exception as e:
        print(f"(error reading key stats: {type(e).__name__}: {e})")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
