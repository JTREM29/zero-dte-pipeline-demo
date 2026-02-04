from __future__ import annotations

import json
import os
from typing import Any


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)) or str(default))
    except Exception:
        return int(default)


def _redis_client():
    try:
        import redis  # type: ignore
    except Exception as exc:  # pragma: no cover
        raise RuntimeError("redis package is required") from exc

    host = (os.getenv("TNT_REDIS_HOST", "127.0.0.1") or "127.0.0.1").strip()
    port = _env_int("TNT_REDIS_PORT", 6379)
    db = _env_int("TNT_REDIS_DB", 0)
    return redis.Redis(host=host, port=port, db=db, decode_responses=True)


def _scan_keys(r, pattern: str, limit: int = 500) -> list[str]:
    keys: list[str] = []
    cursor = 0
    while True:
        cursor, batch = r.scan(cursor=cursor, match=pattern, count=200)
        keys.extend(batch)
        if cursor == 0 or len(keys) >= limit:
            break
    return sorted(keys)[:limit]


def main() -> int:
    r = _redis_client()

    try:
        r.ping()
    except Exception as exc:
        host = os.getenv("TNT_REDIS_HOST", "127.0.0.1")
        port = os.getenv("TNT_REDIS_PORT", "6379")
        db = os.getenv("TNT_REDIS_DB", "0")
        print(
            "Redis is not reachable. Set TNT_REDIS_HOST/TNT_REDIS_PORT/TNT_REDIS_DB to the correct server, then retry."
        )
        print(f"Connection target: {host}:{port} db={db}")
        print(f"Error: {type(exc).__name__}: {exc}")
        return 2

    active = sorted(list(r.smembers("alerts:active")))
    print("SMEMBERS alerts:active")
    print(json.dumps(active, indent=2))

    print("\nSCAN match=alert:*")
    keys = _scan_keys(r, "alert:*")
    print(json.dumps(keys, indent=2))

    # Helpful per-alert summary
    if active:
        print("\nPer-alert summary")
        for alert_id in active[:50]:
            meta_key = f"alert:{alert_id}:meta"
            intent_key = f"alert:{alert_id}:intent"
            meta: dict[str, Any] = r.hgetall(meta_key) or {}
            intent_raw = r.get(intent_key)
            ok_intent = bool(intent_raw)
            try:
                intent = json.loads(intent_raw) if intent_raw else None
            except Exception:
                intent = None

            tf = None
            try:
                tf = (intent or {}).get("condition", {}).get("timeframe")
            except Exception:
                tf = None

            print(
                json.dumps(
                    {
                        "alert_id": alert_id,
                        "meta": {"status": meta.get("status"), "created_at": meta.get("created_at")},
                        "has_intent": ok_intent,
                        "timeframe": tf,
                    },
                    ensure_ascii=False,
                )
            )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
