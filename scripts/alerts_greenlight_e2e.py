from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone, timedelta
from typing import Any

from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from delivery import market_data_adapter
from tnt_alerts.eval_engine import Bar, GateContext, MarketDataSnapshot, evaluate_alert
from tnt_alerts.llm_compiler.validate import validate_intent
from tnt_alerts.storage.redis_store import AlertStore


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)) or str(default))
    except Exception:
        return default


def _redis_client():
    try:
        import redis  # type: ignore
    except Exception as exc:  # pragma: no cover
        raise RuntimeError("redis package is required") from exc

    host = (os.getenv("TNT_REDIS_HOST", "127.0.0.1") or "127.0.0.1").strip()
    port = _env_int("TNT_REDIS_PORT", 6379)
    db = _env_int("TNT_REDIS_DB", 0)
    return redis.Redis(host=host, port=port, db=db, decode_responses=True)


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


def _build_snapshot(symbol: str, tf: str, *, lookback: int) -> MarketDataSnapshot:
    sym = (symbol or "").strip().upper()

    def _fetch(tf2: str):
        try:
            return market_data_adapter.get_ohlc(sym, tf=tf2, limit=int(lookback))
        except Exception:
            return None

    # Try a few common variants; many local DBs only have 1m.
    tried: list[str] = []
    bars_raw = None
    for tf2 in [tf, tf.strip().lower(), tf.strip().upper(), "1m"]:
        tf2 = str(tf2).strip()
        if not tf2 or tf2 in tried:
            continue
        tried.append(tf2)
        ohlc = _fetch(tf2)
        bars_raw = ohlc.get("bars") if isinstance(ohlc, dict) else None
        if isinstance(bars_raw, list) and bars_raw:
            tf = tf2
            break

    if not isinstance(bars_raw, list) or not bars_raw:
        raise RuntimeError(f"No bars available for {sym} (tried tf={tried})")

    bars: list[Bar] = []
    for b in bars_raw:
        if not isinstance(b, dict):
            continue
        ts = _parse_iso(str(b.get("ts") or ""))
        if ts is None:
            continue
        bars.append(
            Bar(
                ts_utc=ts,
                o=float(b.get("o") or 0.0),
                h=float(b.get("h") or 0.0),
                l=float(b.get("l") or 0.0),
                c=float(b.get("c") or 0.0),
                v=float(b.get("v") or 0.0),
            )
        )

    if len(bars) < 2:
        raise RuntimeError(f"Need >=2 bars for deterministic touch trigger, got {len(bars)}")

    return MarketDataSnapshot(
        symbol=sym,
        timeframe=str(tf),
        bars=bars,
        last_price=float(bars[-1].c),
        price_ts_utc=bars[-1].ts_utc,
    )


def _is_expired(intent: dict[str, Any], meta: dict[str, str], now_utc: datetime) -> bool:
    lifecycle = intent.get("lifecycle") if isinstance(intent.get("lifecycle"), dict) else {}
    exp = lifecycle.get("expires") if isinstance(lifecycle.get("expires"), dict) else None
    if not isinstance(exp, dict):
        return False

    created_at = _parse_iso(str(meta.get("created_at_utc") or ""))
    if created_at is None:
        return False

    exp_type = str(exp.get("type") or "").strip().lower()
    if exp_type == "date":
        # Date expiries are inclusive by date; expire when now date is past.
        d = exp.get("date")
        try:
            dt = datetime.fromisoformat(str(d)).date()
        except Exception:
            return False
        return now_utc.date() > dt

    if exp_type != "relative":
        return False

    minutes = exp.get("minutes")
    hours = exp.get("hours")
    days = exp.get("days")
    try:
        delta = timedelta(
            minutes=int(minutes) if minutes is not None else 0,
            hours=int(hours) if hours is not None else 0,
            days=int(days) if days is not None else 0,
        )
    except Exception:
        return False

    if delta.total_seconds() <= 0:
        return False

    return now_utc >= (created_at + delta)


def _build_guaranteed_touch_intent(*, symbol: str, tf: str, channel_id: str, user_id: str, level: float, cooldown_sec: int, expires_minutes: int) -> dict[str, Any]:
    now = datetime.now(timezone.utc).isoformat()
    return {
        "version": "1.0",
        "source": {
            "user_id": user_id,
            "channel_id": channel_id,
            "request_text": f"[GREENLIGHT] {symbol} touch {level} intrabar on {tf}",
            "created_at_utc": now,
        },
        "targets": {"type": "symbols", "symbols": [symbol], "watchlist": None, "max_symbols": 1},
        "condition": {
            "type": "touch",
            "timeframe": tf,
            "confirm": "intrabar",
            "left": {"type": "price", "field": "last"},
            "right": {"type": "number", "value": float(level)},
        },
        "gates": {
            "market_hours": {"session": "RTH"},
            "cooldown": {"seconds": int(cooldown_sec)},
            "max_triggers": {"count": 10},
        },
        "lifecycle": {
            "start": "now",
            "expires": {"type": "relative", "minutes": int(expires_minutes)},
        },
        "actions": [{"type": "discord_notify", "style": "compact"}],
        "tags": {"greenlight": True},
    }


def _enqueue_discord_job(*, r, queue_key: str, job: dict[str, Any]) -> None:
    raw = json.dumps(job, separators=(",", ":"), ensure_ascii=False)
    r.rpush(queue_key, raw)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Green-light E2E alert trigger test (guaranteed fire).")
    p.add_argument("--symbol", default="SPY")
    p.add_argument("--tf", default="5m")
    p.add_argument("--lookback", type=int, default=300)
    p.add_argument("--seconds", type=int, default=90)
    p.add_argument("--poll-sec", type=float, default=2.0)
    p.add_argument("--cooldown-sec", type=int, default=60)
    p.add_argument("--expires-min", type=int, default=1)
    p.add_argument("--enqueue", action="store_true", help="enqueue alert_trigger jobs into TNT_REDIS_QUEUE")
    p.add_argument("--enqueue-discord", action="store_true", help="enqueue alert_trigger jobs directly into TNT_ALERTS_DISCORD_QUEUE")
    p.add_argument(
        "--stop-after-first-trigger",
        action="store_true",
        help="Stop immediately after the first TRIGGERED decision (safest for canary runs).",
    )
    p.add_argument(
        "--max-enqueues",
        type=int,
        default=0,
        help="If >0, stop after enqueuing this many jobs (across --enqueue/--enqueue-discord).",
    )
    p.add_argument("--channel-id", default=os.getenv("DISCORD_CANARY_CHANNEL_ID", "") or "")
    p.add_argument("--user-id", default="discord:greenlight")
    p.add_argument("--session", default="RTH", choices=["RTH", "ETH", "CUSTOM"], help="market_hours session gate")
    p.add_argument("--window", default="", help="CUSTOM window ET like 09:30-16:00 (only when --session=CUSTOM)")

    args = p.parse_args(argv)

    symbol = str(args.symbol).strip().upper() or "SPY"
    tf = str(args.tf).strip() or "5m"
    channel_id = str(args.channel_id).strip()
    if (args.enqueue or args.enqueue_discord) and not channel_id:
        raise SystemExit("--channel-id required when --enqueue/--enqueue-discord is set (or set DISCORD_CANARY_CHANNEL_ID).")

    r = _redis_client()
    store = AlertStore(r)

    queue_key = (os.getenv("TNT_REDIS_QUEUE", "tnt:jobs") or "tnt:jobs").strip()
    discord_queue_key = (os.getenv("TNT_ALERTS_DISCORD_QUEUE", "tnt:alerts:discord_queue") or "tnt:alerts:discord_queue").strip()

    snap = _build_snapshot(symbol, tf, lookback=int(args.lookback))
    # Guarantee: bar close is always within [low, high].
    lvl = float(snap.bars[-1].c)

    intent = _build_guaranteed_touch_intent(
        symbol=symbol,
        tf=tf,
        channel_id=channel_id or "0",
        user_id=str(args.user_id),
        level=lvl,
        cooldown_sec=int(args.cooldown_sec),
        expires_minutes=int(args.expires_min),
    )

    # Override session/window if requested.
    try:
        gates = intent.get("gates") if isinstance(intent.get("gates"), dict) else {}
        mh = gates.get("market_hours") if isinstance(gates.get("market_hours"), dict) else {}
        mh2 = dict(mh)
        sess = str(args.session or "RTH").strip().upper()
        mh2["session"] = sess
        if sess == "CUSTOM":
            w = str(args.window or "").strip()
            if w and ("-" in w):
                a, b = w.split("-", 1)
                mh2["time_window_et"] = [a.strip(), b.strip()]
            else:
                # If no window provided, make a narrow window around now (ET)
                # so the canary can be run at any time.
                try:
                    from zoneinfo import ZoneInfo

                    now_et = datetime.now(timezone.utc).astimezone(ZoneInfo("America/New_York"))
                    start = (now_et - timedelta(minutes=5)).strftime("%H:%M")
                    end = (now_et + timedelta(minutes=10)).strftime("%H:%M")
                    mh2["time_window_et"] = [start, end]
                except Exception:
                    mh2["time_window_et"] = ["00:00", "23:59"]
        else:
            mh2["time_window_et"] = None

        gates2 = dict(gates)
        gates2["market_hours"] = mh2
        intent = dict(intent)
        intent["gates"] = gates2
    except Exception:
        pass

    # Validate intent contract before storing.
    _ = validate_intent(intent)

    alert_id = store.next_id()
    store.create_alert(alert_id, intent)

    print(f"[GREENLIGHT] Created {alert_id} symbol={symbol} tf={tf} level={lvl} cooldown_sec={args.cooldown_sec} expires_min={args.expires_min}")

    t_end = time.time() + max(10, int(args.seconds))
    enq = 0
    decisions: dict[str, int] = {}

    while time.time() < t_end:
        now_utc = datetime.now(timezone.utc)

        meta = store.get_meta(alert_id)
        if meta.get("status") != "active":
            print(f"[GREENLIGHT] status={meta.get('status')} -> stopping")
            break

        if _is_expired(intent, meta, now_utc):
            store.expire_alert(alert_id)
            print("[GREENLIGHT] expired -> routing removed")
            break

        # Evaluate once (single symbol).
        state = store.get_state(alert_id, symbol)
        event = evaluate_alert(validate_intent(intent), snap, context={}, gctx=GateContext(now_utc=now_utc), state=state)
        store.set_state(alert_id, symbol, state)

        d = str(event.get("decision") or "")
        decisions[d] = decisions.get(d, 0) + 1

        if d == "TRIGGERED":
            print(f"[GREENLIGHT][TRIGGER] ts={event.get('ts_utc')} alert_id={alert_id} symbol={symbol}")
            if args.enqueue:
                job = {
                    "type": "alert_trigger",
                    "job_type": "alert_trigger",
                    "job_id": f"alert_trigger:{alert_id}:{symbol}:{event.get('ts_utc')}",
                    "alert_id": alert_id,
                    "symbol": symbol,
                    "tf": tf,
                    "ts_utc": event.get("ts_utc"),
                    "intent": intent,
                    "event": event,
                    "actions": intent.get("actions", []),
                    "meta": {"channel_id": channel_id},
                }
                r.rpush(queue_key, json.dumps(job, separators=(",", ":"), ensure_ascii=False))
                enq += 1

            if args.stop_after_first_trigger and enq > 0:
                print("[GREENLIGHT] stop_after_first_trigger=1 -> stopping")
                break

            if args.enqueue_discord:
                job2 = {
                    "type": "alert_trigger",
                    "job_type": "alert_trigger",
                    "job_id": f"alert_trigger:{alert_id}:{symbol}:{event.get('ts_utc')}",
                    "alert_id": alert_id,
                    "symbol": symbol,
                    "tf": tf,
                    "ts_utc": event.get("ts_utc"),
                    "intent": intent,
                    "event": event,
                    "actions": intent.get("actions", []),
                    "meta": {"channel_id": channel_id},
                }
                _enqueue_discord_job(r=r, queue_key=discord_queue_key, job=job2)
                enq += 1

            if args.stop_after_first_trigger and enq > 0:
                print("[GREENLIGHT] stop_after_first_trigger=1 -> stopping")
                break

            if int(args.max_enqueues or 0) > 0 and enq >= int(args.max_enqueues or 0):
                print(f"[GREENLIGHT] max_enqueues={int(args.max_enqueues)} reached -> stopping")
                break

        time.sleep(float(args.poll_sec))

    # Verify routing membership removal on expiry.
    active_ids = store.list_tf_alerts(tf)
    in_tf = alert_id in active_ids
    meta2 = store.get_meta(alert_id)

    print(
        f"[GREENLIGHT] decisions={decisions} enqueued={enq} status={meta2.get('status')} in_tf_set={in_tf} "
        f"enqueue_queue={queue_key} enqueue_discord_queue={discord_queue_key}"
    )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
