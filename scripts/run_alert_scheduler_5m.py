from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone
from typing import Any, Iterable

from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from delivery import market_data_adapter
from tnt_alerts.eval_engine import Bar, GateContext, MarketDataSnapshot, evaluate_alert
from tnt_alerts.llm_compiler.validate import validate_intent
from tnt_alerts.storage.redis_store import AlertStore
from tnt_alerts.watchlists import load_watchlist_symbols

from services.calendar.calendar_service import CalendarService
from services.calendar.static_macro_calendar import upcoming_events
from services.news.news_service import NewsService


SEEDED_MACRO = False


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


def _tf_db(tf: str) -> str:
    # Local DB uses "1d" not "1D".
    t = (tf or "1m").strip()
    if t.lower() == "1d":
        return "1d"
    return t


def _build_snapshot(symbol: str, tf: str, *, lookback: int) -> MarketDataSnapshot | None:
    sym = (symbol or "").strip().upper()
    if not sym:
        return None

    ohlc = market_data_adapter.get_ohlc(sym, tf=_tf_db(tf), limit=int(lookback))
    bars_raw = ohlc.get("bars") if isinstance(ohlc, dict) else None
    if not isinstance(bars_raw, list) or not bars_raw:
        return None

    bars: list[Bar] = []
    for b in bars_raw:
        if not isinstance(b, dict):
            continue
        ts = _parse_iso(str(b.get("ts") or ""))
        if ts is None:
            continue
        try:
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
        except Exception:
            continue

    if not bars:
        return None

    return MarketDataSnapshot(
        symbol=sym,
        timeframe=str(tf),
        bars=bars,
        last_price=float(bars[-1].c),
        price_ts_utc=bars[-1].ts_utc,
    )


def _fetch_snaps(symbols: Iterable[str], tf: str, *, lookback: int) -> dict[str, MarketDataSnapshot]:
    out: dict[str, MarketDataSnapshot] = {}
    for s in symbols:
        snap = _build_snapshot(s, tf, lookback=lookback)
        if snap is not None:
            out[snap.symbol] = snap
    return out


def _build_levels_context(symbol: str) -> dict:
    sym = (symbol or "").strip().upper()
    ctx: dict[str, Any] = {}

    try:
        piv = market_data_adapter.get_pivots(sym)
        piv_map = (piv or {}).get("piv") if isinstance(piv, dict) else None
        if isinstance(piv_map, dict) and piv_map:
            ctx["pivots"] = piv_map
    except Exception:
        pass

    # Optional y_high / y_low from daily bars (best-effort).
    try:
        from delivery.discord_bot_head import get_last_n_bars

        rows = get_last_n_bars(sym, tf="1d", n=3)
        if len(rows) >= 2:
            _ts, _o, high, low, _c = rows[-2]
            ctx["y_high"] = float(high)
            ctx["y_low"] = float(low)
    except Exception:
        pass

    return ctx


def _expand_targets(intent: dict[str, Any]) -> list[str]:
    targets = intent.get("targets") if isinstance(intent.get("targets"), dict) else {}
    src = intent.get("source") if isinstance(intent.get("source"), dict) else {}

    ttype = str(targets.get("type") or "symbols").strip().lower()
    max_symbols = int(targets.get("max_symbols") or 20)

    if ttype == "watchlist":
        name = str(targets.get("watchlist") or "default").strip() or "default"
        user_id = str(src.get("user_id") or "").strip()
        syms = load_watchlist_symbols(user_id=user_id, name=name)
        return syms[:max(1, min(max_symbols, 20))]

    raw = targets.get("symbols") if isinstance(targets.get("symbols"), list) else []
    syms: list[str] = []
    for s in raw:
        s2 = (str(s) if s is not None else "").strip().upper()
        if s2:
            syms.append(s2)
    return syms[:max(1, min(max_symbols, 20))]


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Run the TNT alert scheduler loop for 5 minutes (sanity test).")
    p.add_argument("--tf", default="5m")
    p.add_argument("--lookback", type=int, default=300)
    p.add_argument("--seconds", type=int, default=300)
    p.add_argument("--poll-sec", type=float, default=10.0)
    p.add_argument(
        "--enqueue",
        action="store_true",
        help="If set, enqueue alert_trigger jobs into TNT_REDIS_QUEUE (default is log-only).",
    )

    args = p.parse_args(argv)

    tf = str(args.tf).strip() or "5m"
    lookback = int(args.lookback)
    seconds = int(args.seconds)
    poll_sec = float(args.poll_sec)

    r = _redis_client()
    store = AlertStore(r)

    calendar = CalendarService(store.r)
    news = NewsService(store.r)

    global SEEDED_MACRO
    if not SEEDED_MACRO:
        try:
            calendar.set_macro_upcoming(upcoming_events(datetime.now(timezone.utc)))
        except Exception:
            pass
        SEEDED_MACRO = True

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

    queue_key = (os.getenv("TNT_REDIS_QUEUE", "tnt:jobs") or "tnt:jobs").strip()
    print(
        f"[TNT][ALERTS][SCHED_5M] tf={tf} lookback={lookback} seconds={seconds} poll_sec={poll_sec} enqueue={bool(args.enqueue)} redis_queue={queue_key}"
    )

    t_end = time.time() + max(5, seconds)
    ticks = 0
    evals = 0
    triggers = 0

    while time.time() < t_end:
        ticks += 1
        now_utc = datetime.now(timezone.utc)

        # Ops heartbeat (best-effort).
        try:
            r.set("tnt:alerts:scheduler:last_tick_utc", now_utc.isoformat())
            r.set("tnt:alerts:scheduler:last_tf", str(tf))
        except Exception:
            pass

        alert_ids = store.list_tf_alerts(tf)
        if not alert_ids:
            print(f"[TNT][ALERTS][TICK] {now_utc.isoformat()} tf={tf} alerts=0")
            time.sleep(poll_sec)
            continue

        # Load intents + meta
        active: list[tuple[str, dict[str, Any], dict[str, str]]] = []
        for aid in alert_ids:
            meta = store.get_meta(aid)
            if meta.get("status") != "active":
                continue
            intent = store.get_intent(aid)
            if isinstance(intent, dict):
                active.append((aid, intent, meta))

        sym_to_alerts: dict[str, list[tuple[str, dict[str, Any]]]] = {}
        for aid, intent_dict, _meta in active:
            try:
                # Validates and normalizes enums, etc.
                _ = validate_intent(intent_dict)
            except Exception as exc:
                print(f"[TNT][ALERTS][WARN] invalid intent alert_id={aid}: {type(exc).__name__}: {exc}")
                continue

            for sym in _expand_targets(intent_dict):
                sym_to_alerts.setdefault(sym, []).append((aid, intent_dict))

        if not sym_to_alerts:
            print(f"[TNT][ALERTS][TICK] {now_utc.isoformat()} tf={tf} alerts=0 (after expand)")
            time.sleep(poll_sec)
            continue

        snaps = _fetch_snaps(sym_to_alerts.keys(), tf, lookback=lookback)
        gctx = GateContext(now_utc=now_utc, regime=None, regime_confidence=None)

        # Inject optional services for gate evaluation.
        gctx.calendar = calendar
        gctx.news = news

        print(f"[TNT][ALERTS][TICK] {now_utc.isoformat()} tf={tf} symbols={len(sym_to_alerts)}")

        for sym, items in sym_to_alerts.items():
            snap = snaps.get(sym)
            if snap is None:
                continue
            ctx = _build_levels_context(sym)

            for aid, intent_dict in items:
                evals += 1
                try:
                    intent_obj = validate_intent(intent_dict)
                except Exception:
                    continue

                state = store.get_state(aid, sym)
                event = evaluate_alert(intent_obj, snap, ctx, gctx, state)
                store.set_state(aid, sym, state)

                decision = str(event.get("decision") or "")
                if decision and decision != "TRIGGERED":
                    try:
                        r.set("tnt:alerts:last_skip_utc", str(event.get("ts_utc") or now_utc.isoformat()))
                        r.set("tnt:alerts:last_skip_alert_id", str(aid))
                        r.set("tnt:alerts:last_skip_symbol", str(sym))
                        r.set("tnt:alerts:last_skip_tf", str(tf))
                        r.set("tnt:alerts:last_skip_decision", str(decision))
                        reasons = event.get("reason_codes")
                        if isinstance(reasons, list):
                            r.set("tnt:alerts:last_skip_reason_codes", json.dumps(reasons, separators=(",", ":"), ensure_ascii=False))
                        elif reasons:
                            r.set("tnt:alerts:last_skip_reason_codes", str(reasons))
                    except Exception:
                        pass
                if decision == "TRIGGERED":
                    triggers += 1
                    print(f"[TNT][ALERTS][TRIGGER] alert_id={aid} symbol={sym} tf={tf} ts={event.get('ts_utc')}")

                    try:
                        r.set("tnt:alerts:last_trigger_utc", str(event.get("ts_utc") or now_utc.isoformat()))
                        r.set("tnt:alerts:last_trigger_alert_id", str(aid))
                        r.set("tnt:alerts:last_trigger_symbol", str(sym))
                        r.set("tnt:alerts:last_trigger_tf", str(tf))
                    except Exception:
                        pass

                    if args.enqueue:
                        job = {
                            "type": "alert_trigger",
                            "job_type": "alert_trigger",
                            "job_id": f"alert_trigger:{aid}:{sym}:{event.get('ts_utc')}",
                            "alert_id": aid,
                            "symbol": sym,
                            "ts_utc": event.get("ts_utc"),
                            "intent": intent_dict,
                            "event": event,
                            "actions": intent_dict.get("actions", []),
                        }
                        try:
                            r.rpush(queue_key, json.dumps(job, separators=(",", ":"), ensure_ascii=False))
                        except Exception as exc:
                            print(f"[TNT][ALERTS][WARN] enqueue failed: {type(exc).__name__}: {exc}")

        time.sleep(poll_sec)

        try:
            r.set("tnt:alerts:scheduler:last_enqueued", str(triggers))
        except Exception:
            pass

    print(f"[TNT][ALERTS][DONE] ticks={ticks} evals={evals} triggers={triggers}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
