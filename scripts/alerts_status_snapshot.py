from __future__ import annotations

import os
from datetime import datetime, timezone


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)) or str(default))
    except Exception:
        return default


def _parse_iso(ts: str) -> datetime | None:
    if not ts:
        return None
    try:
        raw = ts.strip().replace("Z", "+00:00")
        dt = datetime.fromisoformat(raw)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        return None


def _age_s(ts: str) -> int | None:
    dt = _parse_iso(ts)
    if dt is None:
        return None
    try:
        return int((datetime.now(timezone.utc) - dt).total_seconds())
    except Exception:
        return None


def main() -> int:
    try:
        import redis  # type: ignore
    except Exception as exc:
        raise SystemExit(f"redis package required: {type(exc).__name__}: {exc}")

    host = (os.getenv("TNT_REDIS_HOST", "127.0.0.1") or "127.0.0.1").strip()
    port = _env_int("TNT_REDIS_PORT", 6379)
    db = _env_int("TNT_REDIS_DB", 0)
    queue_key = (os.getenv("TNT_REDIS_QUEUE", "tnt:jobs") or "tnt:jobs").strip()
    results_key = (os.getenv("TNT_REDIS_RESULTS", "tnt:results") or "tnt:results").strip()

    r = redis.Redis(host=host, port=int(port), db=int(db), decode_responses=True)

    def get(key: str) -> str:
        try:
            return str(r.get(key) or "").strip()
        except Exception:
            return ""

    def llen(key: str) -> int | None:
        try:
            return int(r.llen(key))
        except Exception:
            return None

    # Counts
    try:
        active_n = int(r.scard("alerts:active"))
    except Exception:
        active_n = None

    q_depth = llen(queue_key)
    res_depth = llen(results_key)

    sched_ts = get("tnt:alerts:scheduler:last_tick_utc")
    worker_ts = get("tnt:alerts:worker:last_job_utc")
    bot_ts = get("tnt:alerts:bot:last_delivery_utc")

    last_skip_ts = get("tnt:alerts:last_skip_utc")
    last_skip_decision = get("tnt:alerts:last_skip_decision")
    last_skip_reasons = get("tnt:alerts:last_skip_reason_codes")

    print("== Alerts status snapshot ==")
    print(f"redis={host}:{port}/{db}")
    print(f"counts: active={active_n if active_n is not None else '?'} queue={q_depth if q_depth is not None else '?'} results={res_depth if res_depth is not None else '?'}")
    print(f"scheduler: last={sched_ts or '?'} age_s={_age_s(sched_ts) if sched_ts else '?'} tf={get('tnt:alerts:scheduler:last_tf') or '?'} enq={get('tnt:alerts:scheduler:last_enqueued') or '?'}")
    print(f"worker:    last={worker_ts or '?'} age_s={_age_s(worker_ts) if worker_ts else '?'} job={get('tnt:alerts:worker:last_job_id') or '?'} alert={get('tnt:alerts:worker:last_alert_id') or '?'}")
    print(f"bot:       last={bot_ts or '?'} age_s={_age_s(bot_ts) if bot_ts else '?'} job={get('tnt:alerts:bot:last_job_id') or '?'} alert={get('tnt:alerts:bot:last_alert_id') or '?'}")
    print(
        "last skip: "
        + f"ts={last_skip_ts or '?'} age_s={_age_s(last_skip_ts) if last_skip_ts else '?'} "
        + f"decision={last_skip_decision or '?'} reasons={last_skip_reasons or '?'}"
    )
    print(f"last trig: ts={get('tnt:alerts:last_trigger_utc') or '?'} alert={get('tnt:alerts:last_trigger_alert_id') or '?'} sym={get('tnt:alerts:last_trigger_symbol') or '?'} tf={get('tnt:alerts:last_trigger_tf') or '?'}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
