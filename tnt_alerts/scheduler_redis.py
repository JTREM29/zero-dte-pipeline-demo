from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone
from dataclasses import dataclass
from typing import Callable, Iterable, Mapping, Sequence

from .models import AlertIntentV1
from .store import expand_watchlist_symbols


TIMEFRAMES: list[str] = ["1m", "2m", "3m", "5m", "10m", "15m", "30m", "60m", "1D"]


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)) or str(default))
    except Exception:
        return default


def _redis_client():
    try:
        import redis  # type: ignore
    except Exception as exc:  # pragma: no cover
        raise RuntimeError(
            "redis package is required for scheduler_redis. Install into your active venv."
        ) from exc

    host = (os.getenv("TNT_REDIS_HOST", "127.0.0.1") or "127.0.0.1").strip()
    port = _env_int("TNT_REDIS_PORT", 6379)
    db = _env_int("TNT_REDIS_DB", 0)
    return redis.Redis(host=host, port=port, db=db, decode_responses=True)


@dataclass(frozen=True)
class SchedulerConfig:
    queue: str = "tnt:jobs"


# ---- Redis keys (v1) --------------------------------------------------------
# alerts:active -> set(alert_id)
# alerts:tf:<tf> -> set(alert_id)
# alert:<id>:intent -> json
# alert:<id>:state:<symbol> -> json


def tf_key(tf: str) -> str:
    return f"alerts:tf:{tf}"


def alert_intent_key(alert_id: str) -> str:
    return f"alert:{alert_id}:intent"


def alert_state_key(alert_id: str, symbol: str) -> str:
    return f"alert:{alert_id}:state:{symbol}"


def _alert_status_key(alert_id: str) -> str:
    return f"alert:{alert_id}:status"


def store_alert_intent(*, alert_id: str, intent: AlertIntentV1) -> None:
    r = _redis_client()
    r.sadd("alerts:active", alert_id)
    tf = str(getattr(intent.condition, "timeframe", "5m"))
    r.sadd(tf_key(tf), alert_id)
    r.set(alert_intent_key(alert_id), json.dumps(intent.model_dump(mode="json"), separators=(",", ":")))
    # Best-effort status marker.
    r.set(_alert_status_key(alert_id), "active")


def pause_alert(alert_id: str) -> None:
    """Stop scheduling an alert (does not delete intent/state)."""

    r = _redis_client()
    tf = None
    blob = r.get(alert_intent_key(alert_id))
    if blob:
        try:
            obj = json.loads(blob)
            tf = (obj.get("condition") or {}).get("timeframe")
        except Exception:
            tf = None

    r.srem("alerts:active", alert_id)
    if tf:
        r.srem(tf_key(str(tf)), alert_id)
    r.set(_alert_status_key(alert_id), "paused")


def resume_alert(alert_id: str) -> None:
    """Resume scheduling an alert (requires stored intent)."""

    r = _redis_client()
    blob = r.get(alert_intent_key(alert_id))
    if not blob:
        return
    try:
        obj = json.loads(blob)
        tf = (obj.get("condition") or {}).get("timeframe") or "5m"
    except Exception:
        tf = "5m"

    r.sadd("alerts:active", alert_id)
    r.sadd(tf_key(str(tf)), alert_id)
    r.set(_alert_status_key(alert_id), "active")


def delete_alert(alert_id: str) -> None:
    """Remove from scheduling sets (keeps keys for forensic recovery)."""

    r = _redis_client()
    tf = None
    blob = r.get(alert_intent_key(alert_id))
    if blob:
        try:
            obj = json.loads(blob)
            tf = (obj.get("condition") or {}).get("timeframe")
        except Exception:
            tf = None

    r.srem("alerts:active", alert_id)
    if tf:
        r.srem(tf_key(str(tf)), alert_id)
    r.set(_alert_status_key(alert_id), "deleted")


def load_alert_intents(alert_ids: Iterable[str]) -> list[tuple[str, AlertIntentV1]]:
    r = _redis_client()
    ids = list(dict.fromkeys([str(x) for x in alert_ids]))
    if not ids:
        return []

    keys = [alert_intent_key(i) for i in ids]
    raw = r.mget(keys)

    out: list[tuple[str, AlertIntentV1]] = []
    for alert_id, blob in zip(ids, raw, strict=True):
        if not blob:
            continue
        try:
            out.append((alert_id, AlertIntentV1.model_validate_json(blob)))
        except Exception:
            continue
    return out


def load_state(alert_id: str, symbol: str) -> dict:
    r = _redis_client()
    blob = r.get(alert_state_key(alert_id, symbol))
    if not blob:
        return {}
    try:
        obj = json.loads(blob)
        return obj if isinstance(obj, dict) else {}
    except Exception:
        return {}


def save_state(alert_id: str, symbol: str, state: dict) -> None:
    r = _redis_client()
    r.set(alert_state_key(alert_id, symbol), json.dumps(state, separators=(",", ":")))


def enqueue_actions(*, alert_id: str, symbol: str, intent: AlertIntentV1, event: dict, actions: list[dict]) -> None:
    r = _redis_client()

    host = (os.getenv("TNT_REDIS_HOST", "127.0.0.1") or "127.0.0.1").strip()
    port = _env_int("TNT_REDIS_PORT", 6379)
    db = _env_int("TNT_REDIS_DB", 0)

    queue_key = (os.getenv("TNT_REDIS_QUEUE", "tnt:jobs") or "tnt:jobs").strip()

    job = {
        "type": "alert_trigger",
        "job_id": f"alert_trigger:{alert_id}:{symbol}:{event.get('ts_utc')}",
        "alert_id": alert_id,
        "symbol": symbol,
        "ts_utc": event.get("ts_utc"),
        "intent": {
            "version": intent.version,
            "user_id": intent.source.user_id,
            "channel_id": intent.source.channel_id,
        },
        "event": event,
        "actions": actions,
    }

    r.rpush(queue_key, json.dumps(job, separators=(",", ":")))

    # Proof-grade enqueue marker (connects TRIGGER -> POP -> POSTED).
    try:
        print(
            "[TNT][ALERTS][SCHED][ENQUEUE] "
            + f"redis={host}:{port}/{db} queue={queue_key} alert_id={alert_id} job_id={job.get('job_id')} symbol={symbol}"
        )
    except Exception:
        pass


# ---- Scheduler loop skeleton -------------------------------------------------


FetchSnapsFn = Callable[[Sequence[str], str], Mapping[str, object]]
BuildGateContextFn = Callable[[str], object]
BuildLevelsContextFn = Callable[[str], dict]
ExpandTargetsFn = Callable[[AlertIntentV1], list[str]]
EvaluateFn = Callable[[AlertIntentV1, object, dict, object, dict], dict]


def scheduler_tick(
    *,
    tf: str,
    expand_targets: ExpandTargetsFn,
    fetch_snaps: FetchSnapsFn,
    build_gate_context: BuildGateContextFn,
    build_levels_context: BuildLevelsContextFn,
    evaluate_alert: EvaluateFn,
) -> None:
    """One deterministic tick for a timeframe bucket.

    This function intentionally delegates I/O to injected callables so it can be
    tested and swapped for real pipeline implementations.
    """

    r = _redis_client()

    now_utc = datetime.now(timezone.utc).isoformat()
    # Ops heartbeat (best-effort).
    try:
        r.set("tnt:alerts:scheduler:last_tick_utc", now_utc)
        r.set("tnt:alerts:scheduler:last_tf", str(tf))
    except Exception:
        pass

    alert_ids = list(r.smembers(tf_key(tf)))
    intents = load_alert_intents(alert_ids)

    symbol_to_alerts: dict[str, list[tuple[str, AlertIntentV1]]] = {}
    for alert_id, intent in intents:
        syms = expand_targets(intent)
        for sym in syms:
            symbol_to_alerts.setdefault(sym, []).append((alert_id, intent))

    snaps = fetch_snaps(list(symbol_to_alerts.keys()), tf)

    enqueued = 0

    for sym, alert_list in symbol_to_alerts.items():
        snap = snaps.get(sym)
        if snap is None:
            continue

        gctx = build_gate_context(sym)
        ctx = build_levels_context(sym)

        for alert_id, intent in alert_list:
            state = load_state(alert_id, sym)
            event = evaluate_alert(intent, snap, ctx, gctx, state)
            save_state(alert_id, sym, state)

            decision = str((event or {}).get("decision") or "")
            if decision and decision != "TRIGGERED":
                # Best-effort: capture the most recent non-trigger decision and why.
                try:
                    r.set("tnt:alerts:last_skip_utc", str(event.get("ts_utc") or now_utc))
                    r.set("tnt:alerts:last_skip_alert_id", str(alert_id))
                    r.set("tnt:alerts:last_skip_symbol", str(sym))
                    r.set("tnt:alerts:last_skip_tf", str(tf))
                    r.set("tnt:alerts:last_skip_decision", str(decision))
                    reasons = event.get("reason_codes")
                    if isinstance(reasons, list):
                        r.set("tnt:alerts:last_skip_reason_codes", json.dumps(reasons, separators=(",", ":"), ensure_ascii=False))
                    elif reasons:
                        r.set("tnt:alerts:last_skip_reason_codes", str(reasons))

                    # Optional: store a richer reason detail (first gate reason) for ops UX.
                    try:
                        gates = ((event or {}).get("eval") or {}).get("gates")
                        r0 = gates[0] if isinstance(gates, list) and gates else None
                        if isinstance(r0, dict):
                            headline = str(r0.get("headline") or "").strip()
                            detail = str(r0.get("detail") or "").strip()
                            note = str(r0.get("note") or "").strip()
                            msg = ""
                            if headline:
                                msg = f"Suppressed: {headline}"
                                if detail:
                                    msg += f" ({detail})"
                            elif note:
                                msg = note
                            if msg:
                                r.set("tnt:alerts:last_skip_reason_detail", msg[:240])
                    except Exception:
                        pass
                except Exception:
                    pass

            if decision == "TRIGGERED":
                try:
                    r.set("tnt:alerts:last_trigger_utc", str(event.get("ts_utc") or now_utc))
                    r.set("tnt:alerts:last_trigger_alert_id", str(alert_id))
                    r.set("tnt:alerts:last_trigger_symbol", str(sym))
                    r.set("tnt:alerts:last_trigger_tf", str(tf))
                except Exception:
                    pass
                enqueue_actions(
                    alert_id=alert_id,
                    symbol=sym,
                    intent=intent,
                    event=event,
                    actions=[a.model_dump(mode="json") for a in intent.actions],
                )
                enqueued += 1

    try:
        r.set("tnt:alerts:scheduler:last_enqueued", str(enqueued))
    except Exception:
        pass


def sleep_until_boundary(tf: str) -> None:
    """Sleep until the next close boundary for this timeframe.

    This is intentionally simple; replace with exchange-calendar-aware alignment.
    """

    mult = {
        "1m": 60,
        "2m": 120,
        "3m": 180,
        "5m": 300,
        "10m": 600,
        "15m": 900,
        "30m": 1800,
        "60m": 3600,
        "1D": 86400,
    }.get(tf, 60)

    now = time.time()
    next_tick = (int(now // mult) + 1) * mult
    time.sleep(max(0.0, next_tick - now))


def _parse_user_id_int(user_ref: str) -> int | None:
    if not user_ref:
        return None
    if ":" in user_ref:
        tail = user_ref.split(":", 1)[1]
        try:
            return int(tail)
        except Exception:
            return None
    try:
        return int(user_ref)
    except Exception:
        return None


def expand_targets_default(intent: AlertIntentV1) -> list[str]:
    if intent.targets.type == "symbols":
        return list(intent.targets.symbols or [])[: int(intent.targets.max_symbols or 20)]
    name = (intent.targets.watchlist or "default").strip().lower() or "default"
    syms = expand_watchlist_symbols(watchlist_name=name, user_id_int=_parse_user_id_int(intent.source.user_id))
    return syms[: int(intent.targets.max_symbols or 20)]
