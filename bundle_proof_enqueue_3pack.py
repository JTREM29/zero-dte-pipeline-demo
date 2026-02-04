import argparse
import copy
import json
import os
import sys
from datetime import datetime, timedelta, timezone


def _ensure_repo_on_path() -> None:
    # Make the script runnable from anywhere (so imports like services.* resolve).
    repo = os.path.dirname(os.path.abspath(__file__))
    if repo and repo not in sys.path:
        sys.path.insert(0, repo)


def main() -> None:
    _ensure_repo_on_path()

    from services.redis_env import redis_client
    from tnt_alerts.storage.redis_store import AlertStore

    ap = argparse.ArgumentParser()
    ap.add_argument("--alert-id", default="A00000019")
    ap.add_argument("--symbol", default="SPY")
    ap.add_argument("--tf", default="1m")
    ap.add_argument("--queue", default="tnt:alerts:discord_queue")
    ap.add_argument("--levels", default="694.47,694.48,694.50")
    ap.add_argument("--age0", type=int, default=113, help="starting data_age_sec for the first job")
    args = ap.parse_args()

    r = redis_client(timeout_s=0.5, decode_responses=True)
    store = AlertStore(r)

    alert_id = str(args.alert_id)
    symbol = str(args.symbol)
    tf = str(args.tf)
    q = str(args.queue)

    levels: list[float] = []
    for s in str(args.levels).split(","):
        s = s.strip()
        if not s:
            continue
        levels.append(float(s))

    intent_base = store.get_intent(alert_id) or {}
    if not isinstance(intent_base, dict):
        intent_base = {}

    base = datetime.now(timezone.utc)

    for i, lvl in enumerate(levels):
        ts = (base + timedelta(seconds=i * 2)).isoformat()
        age_s = int(args.age0) + (i * 2)

        intent = copy.deepcopy(intent_base)
        cond = intent.get("condition") if isinstance(intent.get("condition"), dict) else {}
        cond.update({"type": "touch", "confirm": "intrabar"})

        right = cond.get("right") if isinstance(cond.get("right"), dict) else {}
        right.update({"type": right.get("type") or "price", "value": lvl})
        cond["right"] = right
        intent["condition"] = cond

        job = {
            "type": "alert_trigger",
            "job_type": "alert_trigger",
            "job_id": f"bundle_proof:{alert_id}:{symbol}:{ts}",
            "alert_id": alert_id,
            "symbol": symbol,
            "tf": tf,
            "ts_utc": ts,
            "intent": intent,
            "event": {
                "ts_utc": ts,
                "decision": "TRIGGERED",
                "reason_codes": ["TRIGGERED_DEBUG_FORCE"],
                "data_age_sec": age_s,
                "eval": {
                    "condition": {
                        "type": "touch",
                        "level": lvl,
                        "confirm": "intrabar",
                        "curr_left": lvl,
                        "debug_forced": True,
                    }
                },
            },
            "actions": [],
        }

        r.rpush(q, json.dumps(job, separators=(",", ":"), ensure_ascii=False))

    print("enqueued", len(levels), "jobs to", q, "alert_id", alert_id)


if __name__ == "__main__":
    main()
