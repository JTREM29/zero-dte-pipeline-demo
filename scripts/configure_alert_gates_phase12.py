from __future__ import annotations

import json
import os

import redis


def _redis_client() -> redis.Redis:
    host = (os.getenv("TNT_REDIS_HOST", "127.0.0.1") or "127.0.0.1").strip()
    port = int((os.getenv("TNT_REDIS_PORT", "6379") or "6379").strip())
    db = int((os.getenv("TNT_REDIS_DB", "0") or "0").strip())
    return redis.Redis(host=host, port=port, db=db, decode_responses=True)


def _pick_alert_id_for_tf(r: redis.Redis, tf: str) -> str | None:
    tf = (tf or "").strip()
    if not tf:
        return None
    ids = sorted(list(r.smembers(f"alerts:tf:{tf}")))
    return ids[0] if ids else None


def main() -> int:
    r = _redis_client()
    desired_tf = (os.getenv("TNT_VALIDATE_TF", "1m") or "1m").strip() or "1m"
    alert_id = (os.getenv("TNT_VALIDATE_ALERT_ID") or "").strip()
    if not alert_id:
        alert_id = _pick_alert_id_for_tf(r, desired_tf) or ""

    if not alert_id:
        print({"ok": False, "error": "no alert found", "tf": desired_tf})
        return 2

    key = f"alert:{alert_id}:intent"
    raw = r.get(key) or "{}"
    intent = json.loads(raw) if raw else {}
    if not isinstance(intent, dict):
        print({"ok": False, "error": "intent not dict", "alert_id": alert_id})
        return 2

    gates = intent.get("gates")
    if not isinstance(gates, dict):
        gates = {}
        intent["gates"] = gates

    # Configure all three Phase 1/2 gates.
    gates["news_blackout"] = {"minutes": 3, "market_minutes": 5}
    gates["macro_blackout"] = {"event_types": ["CPI"], "pre_minutes": 10, "post_minutes": 10}
    gates["earnings_blackout"] = {"pre_minutes": 10, "post_minutes": 10, "confirmed_only": True}

    r.set(key, json.dumps(intent, separators=(",", ":"), ensure_ascii=False))

    # Debug pointers for follow-up scripts.
    r.set("tnt:debug:last_seed_alert_id", alert_id)
    r.set("tnt:debug:last_seed_tf", desired_tf)

    # Attempt to set symbol too (best-effort).
    sym = ""
    try:
        syms = ((intent.get("targets") or {}).get("symbols") or [])
        sym = (str(syms[0]).strip().upper() if syms else "")
    except Exception:
        sym = ""
    if sym:
        r.set("tnt:debug:last_seed_symbol", sym)

    print({"ok": True, "alert_id": alert_id, "tf": desired_tf, "sym": sym or None, "gates": gates})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
