from __future__ import annotations

import json
import os

import redis


def _redis_client() -> redis.Redis:
    host = (os.getenv("TNT_REDIS_HOST", "127.0.0.1") or "127.0.0.1").strip()
    port = int((os.getenv("TNT_REDIS_PORT", "6379") or "6379").strip())
    db = int((os.getenv("TNT_REDIS_DB", "0") or "0").strip())
    return redis.Redis(host=host, port=port, db=db, decode_responses=True)


def main() -> int:
    r = _redis_client()
    alert_id = (r.get("tnt:debug:last_seed_alert_id") or "").strip()
    sym = (r.get("tnt:debug:last_seed_symbol") or "SPY").strip().upper()
    tf = (r.get("tnt:debug:last_seed_tf") or "").strip()

    print({"seed_alert": alert_id, "sym": sym, "tf": tf or None})
    if not alert_id:
        print("Missing tnt:debug:last_seed_alert_id (run scripts/seed_gates_phase12.py first)")
        return 2

    try:
        intent_raw = r.get(f"alert:{alert_id}:intent") or "{}"
        intent = json.loads(intent_raw) if intent_raw else {}
        gates = (intent.get("gates") or {}) if isinstance(intent, dict) else {}
    except Exception:
        gates = {}

    print({"intent_gates": gates})

    # Verify routing membership.
    try:
        meta = r.hgetall(f"alert:{alert_id}:meta") or {}
        tf_meta = (meta.get("timeframe") or "").strip()
    except Exception:
        tf_meta = ""
    tf_key = f"alerts:tf:{tf_meta}" if tf_meta else ""
    try:
        tf_members = sorted(list(r.smembers(tf_key))) if tf_key else []
    except Exception:
        tf_members = []
    if tf_key:
        print({"tf_key": tf_key, "tf_members_n": len(tf_members), "tf_members_head": tf_members[:10]})

    # Find any stored state keys for this alert.
    found: list[str] = []
    try:
        cursor = 0
        pattern = f"alert:{alert_id}:state:*"
        while True:
            cursor, keys = r.scan(cursor=cursor, match=pattern, count=200)
            for k in keys or []:
                found.append(str(k))
            if int(cursor) == 0:
                break
    except Exception:
        found = []
    found = sorted(set(found))
    print({"state_keys_n": len(found), "state_keys_head": found[:10]})

    key = f"alert:{alert_id}:state:{sym}"
    raw = r.get(key) or ""
    state = json.loads(raw) if raw else {}
    last_eval = state.get("last_eval") if isinstance(state, dict) else None

    print({"state_key": key, "present": bool(raw)})
    print("last_eval=\n" + json.dumps(last_eval, indent=2, sort_keys=True))

    print(
        {
            "telemetry": {
                "last_skip_utc": r.get("tnt:alerts:last_skip_utc"),
                "last_skip_alert_id": r.get("tnt:alerts:last_skip_alert_id"),
                "last_skip_symbol": r.get("tnt:alerts:last_skip_symbol"),
                "last_skip_tf": r.get("tnt:alerts:last_skip_tf"),
                "last_skip_decision": r.get("tnt:alerts:last_skip_decision"),
                "last_skip_reason_codes": r.get("tnt:alerts:last_skip_reason_codes"),
            }
        }
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
