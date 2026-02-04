from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone

import redis


def _redis_client() -> redis.Redis:
    host = (os.getenv("TNT_REDIS_HOST", "127.0.0.1") or "127.0.0.1").strip()
    port = int((os.getenv("TNT_REDIS_PORT", "6379") or "6379").strip())
    db = int((os.getenv("TNT_REDIS_DB", "0") or "0").strip())
    return redis.Redis(host=host, port=port, db=db, decode_responses=True)


def main() -> int:
    r = _redis_client()
    ids = sorted(list(r.smembers("alerts:active")))
    host = r.connection_pool.connection_kwargs.get("host")
    port = r.connection_pool.connection_kwargs.get("port")
    db = r.connection_pool.connection_kwargs.get("db")
    print({"redis": f"{host}:{port}/{db}", "active_n": len(ids), "active_ids_head": ids[:10]})

    if not ids:
        print("No active alerts in alerts:active")
        return 2

    desired_tf = (os.getenv("TNT_VALIDATE_TF", "1m") or "1m").strip()
    if not desired_tf:
        desired_tf = "1m"

    alert_id = ""
    alert_tf = ""
    for cand in ids:
        meta0 = r.hgetall(f"alert:{cand}:meta") or {}
        tf0 = (meta0.get("timeframe") or "").strip()
        if tf0 == desired_tf:
            alert_id = cand
            alert_tf = tf0
            break
    if not alert_id:
        # Fall back to first active alert.
        alert_id = ids[0]
        meta0 = r.hgetall(f"alert:{alert_id}:meta") or {}
        alert_tf = (meta0.get("timeframe") or "").strip()
    intent_raw = r.get(f"alert:{alert_id}:intent") or "{}"
    intent = json.loads(intent_raw) if intent_raw else {}
    meta = r.hgetall(f"alert:{alert_id}:meta") or {}

    syms = ((intent.get("targets") or {}).get("symbols") or [])
    sym = (str(syms[0]).strip().upper() if syms else "SPY")

    try:
        tf_members = sorted(list(r.smembers(f"alerts:tf:{(meta.get('timeframe') or '').strip() or desired_tf}")))
    except Exception:
        tf_members = []

    now = datetime.now(timezone.utc)

    # Phase 2: news gate stamp
    r.set(f"news:symbol:{sym}:last_ts", str(int(now.timestamp())))

    # Phase 1: macro + earnings within 10 minutes
    macro_ts = now + timedelta(minutes=5)
    payload = json.dumps({"type": "CPI", "ts_utc": macro_ts.isoformat()}, separators=(",", ":"))
    r.zadd("cal:macro:upcoming", {payload: float(macro_ts.timestamp())})

    earn_ts = now + timedelta(minutes=5)
    r.set(
        f"cal:earnings:{sym}",
        json.dumps({"ts_utc": earn_ts.isoformat(), "confirmed": True}, separators=(",", ":")),
    )

    r.set("tnt:debug:last_seed_alert_id", alert_id)
    r.set("tnt:debug:last_seed_symbol", sym)
    r.set("tnt:debug:last_seed_tf", alert_tf or (meta.get("timeframe") or ""))

    print(
        {
            "seeded": True,
            "alert_id": alert_id,
            "sym": sym,
            "tf": meta.get("timeframe"),
            "desired_tf": desired_tf,
            "tf_members_head": tf_members[:10],
            "news_ts_utc": now.isoformat(),
            "macro": "CPI",
            "macro_ts_utc": macro_ts.isoformat(),
            "earn_ts_utc": earn_ts.isoformat(),
        }
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
