from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from delivery import market_data_adapter
from tnt_alerts.alert_intent import Session
from tnt_alerts.eval_engine import Bar, GateContext, MarketDataSnapshot, evaluate_alert
from tnt_alerts.llm_compiler.validate import validate_intent
from tnt_alerts.reasons import SUPPRESS_SESSION_MISMATCH
from tnt_alerts.storage.redis_store import AlertStore
from tnt_alerts.watchlists import load_watchlist_symbols

from services.calendar.calendar_service import CalendarService
from services.calendar.static_macro_calendar import upcoming_events
from services.news.news_service import NewsService


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


def _vwap_anchor_for_intent(intent_obj: Any) -> str | None:
    """Return a hint for VWAP anchoring.

    For user expectations, VWAP should reset daily and (when the alert is
    gated to RTH) should be RTH-anchored.
    """

    try:
        mh = getattr(getattr(intent_obj, "gates", None), "market_hours", None)
        if mh is None:
            return "DAY"
        session = getattr(mh, "session", None)
        if session == Session.RTH:
            return "RTH"
        # For CUSTOM windows (and unknown sessions), default to day-reset.
        return "DAY"
    except Exception:
        return None


def _supported_mvp_reason(intent_obj: Any) -> str | None:
    """Return None if supported; otherwise a compact reason string."""

    try:
        def _val(x: object) -> object:
            v = getattr(x, "value", None)
            return v if v is not None else x

        def _s(x: object) -> str:
            if x is None:
                return ""
            return str(_val(x)).strip()

        cond = getattr(intent_obj, "condition", None)
        if cond is None:
            return "missing_condition"

        ctype = _s(getattr(cond, "type", None)).lower()
        if not ctype:
            return "missing_condition_type"

        if ctype == "cross":
            left = getattr(cond, "left", None)
            right = getattr(cond, "right", None)
            if left is None or right is None:
                return "cross_missing_left_or_right"

            ltype = _s(getattr(left, "type", None)).lower()
            rtype = _s(getattr(right, "type", None)).lower()
            if ltype != "price":
                return f"cross_left_type={ltype or 'unknown'}"
            if rtype != "indicator":
                return f"cross_right_type={rtype or 'unknown'}"

            name = _s(getattr(right, "name", None)).lower()
            field = _s(getattr(right, "field", None)).lower()
            if name != "vwap" and field != "vwap":
                return f"cross_indicator={name or field or 'unknown'}"

            return None

        if ctype == "break":
            level = getattr(cond, "level", None)
            if level is None:
                return "break_missing_level"
            ltype = _s(getattr(level, "type", None)).lower()
            if ltype != "ref":
                return f"break_level_type={ltype or 'unknown'}"
            ref = _s(getattr(level, "ref", None)).lower()
            if ref not in {"y_high", "y_low"}:
                return f"break_ref={ref or 'unknown'}"
            return None

        if ctype == "touch":
            # Useful for validation / proving end-to-end loop; keep minimal.
            left = getattr(cond, "left", None)
            right = getattr(cond, "right", None)
            if left is None or right is None:
                return "touch_missing_left_or_right"
            ltype = _s(getattr(left, "type", None)).lower()
            if ltype != "price":
                return f"touch_left_type={ltype or 'unknown'}"
            rtype = _s(getattr(right, "type", None)).lower()
            if rtype != "number":
                return f"touch_right_type={rtype or 'unknown'}"
            return None

        return f"cond_type={ctype}"
    except Exception:
        return "exception_in_supported_check"


def _enqueue_discord_job(*, r: Any, queue_key: str, job: dict) -> None:
    r.rpush(queue_key, json.dumps(job, separators=(",", ":"), ensure_ascii=False))


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Run the MVP live alert evaluator (bucketed by timeframe) and post to Discord test channel via Redis queue.")
    p.add_argument("--tfs", default="1m", help="Comma-separated timeframes (e.g. 1m,5m,15m)")
    p.add_argument("--minutes", type=int, default=10)
    p.add_argument("--lookback", type=int, default=300)
    p.add_argument("--poll-sec", type=float, default=1.0)
    p.add_argument(
        "--max-triggers-total",
        type=int,
        default=None,
        help="Stop after N total triggers across all alerts (default: 1). Use 0 for unlimited.",
    )
    p.add_argument("--stop-after-first-trigger", action="store_true", default=True)
    p.add_argument("--no-stop-after-first-trigger", action="store_false", dest="stop_after_first_trigger")
    p.add_argument("--enqueue-discord", action="store_true", default=True)
    p.add_argument("--no-enqueue-discord", action="store_false", dest="enqueue_discord")

    args = p.parse_args(argv)

    tfs = [x.strip() for x in str(args.tfs or "").split(",") if x.strip()]
    if not tfs:
        tfs = ["1m"]

    try:
        from services.redis_env import redis_client

        r = redis_client(timeout_s=0.5, decode_responses=True)
    except Exception as exc:
        print(f"[TNT][ALERTS][MVP] Redis unavailable: {type(exc).__name__}: {exc}")
        return 2

    try:
        r.ping()
    except Exception as exc:
        host = os.getenv("TNT_REDIS_HOST", "127.0.0.1")
        port = os.getenv("TNT_REDIS_PORT", "6379")
        db = os.getenv("TNT_REDIS_DB", "0")
        print("[TNT][ALERTS][MVP] Redis is not reachable. Set TNT_REDIS_HOST/TNT_REDIS_PORT/TNT_REDIS_DB.")
        print(f"[TNT][ALERTS][MVP] Connection target: {host}:{port} db={db}")
        print(f"[TNT][ALERTS][MVP] Error: {type(exc).__name__}: {exc}")
        return 2

    store = AlertStore(r)

    calendar = CalendarService(store.r)
    news = NewsService(store.r)
    try:
        calendar.set_macro_upcoming(upcoming_events(datetime.now(timezone.utc)))
    except Exception:
        pass

    discord_queue_key = (os.getenv("TNT_ALERTS_DISCORD_QUEUE", "tnt:alerts:discord_queue") or "tnt:alerts:discord_queue").strip()

    max_triggers_total: int
    if args.max_triggers_total is not None:
        max_triggers_total = int(args.max_triggers_total)
    else:
        max_triggers_total = 1 if bool(args.stop_after_first_trigger) else 0
    if max_triggers_total < 0:
        max_triggers_total = 0

    print(
        f"[TNT][ALERTS][MVP] tfs={tfs} minutes={int(args.minutes)} lookback={int(args.lookback)} poll_sec={float(args.poll_sec)} "
        f"enqueue_discord={bool(args.enqueue_discord)} discord_queue={discord_queue_key} stop_after_first_trigger_flag={bool(args.stop_after_first_trigger)} "
        f"max_triggers_total={max_triggers_total} max_triggers_total_explicit={args.max_triggers_total is not None}"
    )

    t_end = time.time() + max(30, int(args.minutes) * 60)
    last_bucket_minute: dict[str, int] = {}

    triggered_any = False
    total_triggers = 0

    while time.time() < t_end:
        now_utc = datetime.now(timezone.utc)
        now_min = int(now_utc.timestamp() // 60)

        # Ops heartbeat (best-effort).
        try:
            r.set("tnt:alerts:mvp:last_tick_utc", now_utc.isoformat())
        except Exception:
            pass

        for tf in tfs:
            # Bucket schedule: run a tf at most once per its bar boundary.
            # 1m => every minute, 5m => every 5 minutes, etc.
            period_min = 1
            if tf.endswith("m"):
                try:
                    period_min = max(1, int(tf[:-1]))
                except Exception:
                    period_min = 1
            elif tf.upper() == "1D":
                period_min = 60 * 24

            if (now_min % period_min) != 0:
                continue

            if last_bucket_minute.get(tf) == now_min:
                continue
            last_bucket_minute[tf] = now_min

            # Load intents + meta
            alert_ids = store.list_tf_alerts(tf)
            if not alert_ids:
                print(f"[TNT][ALERTS][MVP][LOAD] tf={tf} alerts=0")
                continue

            active: list[tuple[str, dict[str, Any], dict[str, str]]] = []
            for aid in alert_ids:
                meta = store.get_meta(aid)
                if meta.get("status") != "active":
                    continue
                intent = store.get_intent(aid)
                if isinstance(intent, dict):
                    active.append((aid, intent, meta))

            print(f"[TNT][ALERTS][MVP][LOAD] tf={tf} alerts={len(alert_ids)} active={len(active)}")

            sym_to_alerts: dict[str, list[tuple[str, dict[str, Any]]]] = {}
            invalid = 0
            unsupported = 0
            unsupported_reasons: dict[str, int] = {}
            for aid, intent_dict, _meta in active:
                try:
                    intent_obj = validate_intent(intent_dict)
                except Exception:
                    invalid += 1
                    continue

                reason = _supported_mvp_reason(intent_obj)
                if reason is not None:
                    unsupported += 1
                    unsupported_reasons[reason] = unsupported_reasons.get(reason, 0) + 1
                    continue

                targets = intent_dict.get("targets") if isinstance(intent_dict.get("targets"), dict) else {}
                ttype = str(targets.get("type") or "symbols").strip().lower() or "symbols"
                max_symbols = int(targets.get("max_symbols") or 20)
                max_symbols = max(1, min(max_symbols, 20))

                syms: list[str] = []
                if ttype == "watchlist":
                    name = str(targets.get("watchlist") or "default").strip() or "default"
                    src = intent_dict.get("source") if isinstance(intent_dict.get("source"), dict) else {}
                    user_id = str(src.get("user_id") or "").strip()
                    try:
                        syms = load_watchlist_symbols(user_id=user_id, name=name)[:max_symbols]
                    except Exception:
                        syms = []
                else:
                    raw_syms = targets.get("symbols") if isinstance(targets.get("symbols"), list) else []
                    for s in raw_syms:
                        sym = (str(s) if s is not None else "").strip().upper()
                        if sym:
                            syms.append(sym)
                    syms = syms[:max_symbols]

                for sym in syms:
                    sym_to_alerts.setdefault(sym, []).append((aid, intent_dict))

            if invalid or unsupported:
                extra = ""
                if unsupported_reasons:
                    top = sorted(unsupported_reasons.items(), key=lambda kv: (-kv[1], kv[0]))[:3]
                    extra = " unsupported_reasons_top=" + ";".join([f"{k}:{v}" for k, v in top])
                print(f"[TNT][ALERTS][MVP][FILTER] tf={tf} invalid={invalid} unsupported={unsupported} symbols={len(sym_to_alerts)}{extra}")

            if not sym_to_alerts:
                continue

            snaps = _fetch_snaps(sym_to_alerts.keys(), tf, lookback=int(args.lookback))
            gctx = GateContext(now_utc=now_utc, regime=None, regime_confidence=None)
            gctx.calendar = calendar
            gctx.news = news

            missing_snaps = max(0, len(sym_to_alerts) - len(snaps))
            print(
                f"[TNT][ALERTS][MVP][TICK] {now_utc.isoformat()} tf={tf} symbols={len(sym_to_alerts)} "
                f"snaps_ok={len(snaps)} snaps_missing={missing_snaps}"
            )

            evals = 0
            triggers = 0
            suppressed = 0
            suppressed_session = 0
            vwap_evals = 0
            vwap_ok = 0
            vwap_missing = 0

            for sym, items in sym_to_alerts.items():
                snap = snaps.get(sym)
                if snap is None:
                    continue

                ctx = _build_levels_context(sym)

                for aid, intent_dict in items:
                    try:
                        intent_obj = validate_intent(intent_dict)
                    except Exception:
                        continue

                    state = store.get_state(aid, sym)

                    # Per-alert context: allow VWAP anchoring to respect session.
                    ctx2 = dict(ctx)
                    vwap_anchor = _vwap_anchor_for_intent(intent_obj)
                    if vwap_anchor:
                        ctx2["vwap_anchor"] = vwap_anchor

                    event = evaluate_alert(intent_obj, snap, ctx2, gctx, state)
                    store.set_state(aid, sym, state)

                    evals += 1

                    if str(event.get("decision") or "") == "SUPPRESSED":
                        suppressed += 1
                        try:
                            gates = ((event.get("eval") or {}).get("gates") or []) if isinstance(event, dict) else []
                            if isinstance(gates, list) and any(isinstance(x, dict) and x.get("code") == SUPPRESS_SESSION_MISMATCH for x in gates):
                                suppressed_session += 1
                        except Exception:
                            pass

                    try:
                        cdet = ((event.get("eval") or {}).get("condition") or {}) if isinstance(event, dict) else {}
                        if isinstance(cdet, dict) and "prev_right" in cdet and "curr_right" in cdet:
                            vwap_evals += 1
                            if cdet.get("prev_right") is None or cdet.get("curr_right") is None:
                                vwap_missing += 1
                            else:
                                vwap_ok += 1
                    except Exception:
                        pass

                    if str(event.get("decision") or "") != "TRIGGERED":
                        continue

                    triggered_any = True
                    triggers += 1
                    total_triggers += 1
                    job = {
                        "type": "alert_trigger",
                        "job_type": "alert_trigger",
                        "job_id": f"alert_trigger:{aid}:{sym}:{event.get('ts_utc')}",
                        "alert_id": aid,
                        "symbol": sym,
                        "tf": tf,
                        "ts_utc": event.get("ts_utc"),
                        "intent": intent_dict,
                        "event": event,
                        "actions": intent_dict.get("actions", []),
                    }

                    print(f"[TNT][ALERTS][MVP][TRIGGER] alert_id={aid} symbol={sym} tf={tf} ts={event.get('ts_utc')}")

                    if args.enqueue_discord:
                        try:
                            _enqueue_discord_job(r=r, queue_key=discord_queue_key, job=job)
                        except Exception as exc:
                            print(f"[TNT][ALERTS][MVP][WARN] enqueue_discord failed: {type(exc).__name__}: {exc}")

                    if max_triggers_total and total_triggers >= max_triggers_total:
                        print(f"[TNT][ALERTS][MVP][STOP] max_triggers_total reached: {total_triggers}")
                        return 0

            print(
                f"[TNT][ALERTS][MVP][SUMMARY] tf={tf} evals={evals} triggers={triggers} "
                f"suppressed={suppressed} suppressed_session={suppressed_session} "
                f"vwap_evals={vwap_evals} vwap_ok={vwap_ok} vwap_missing={vwap_missing} total_triggers={total_triggers}"
            )

        time.sleep(max(0.2, float(args.poll_sec)))

    if not triggered_any:
        print("[TNT][ALERTS][MVP][DONE] No triggers.")
    else:
        print("[TNT][ALERTS][MVP][DONE] Completed with triggers.")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
