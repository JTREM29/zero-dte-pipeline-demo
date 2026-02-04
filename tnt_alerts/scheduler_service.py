from __future__ import annotations

import argparse
import os
from datetime import datetime, timezone, timedelta
from typing import Mapping, Sequence

from delivery import market_data_adapter
from tnt_alerts import scheduler_redis
from tnt_alerts.alert_intent import AlertIntent
from tnt_alerts.eval_engine import Bar, GateContext, MarketDataSnapshot, evaluate_alert


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
    # DB uses "1d" not "1D".
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

    # Deterministic scheduler: rely on stored bars only.
    last_price = float(bars[-1].c)
    ts_dt = bars[-1].ts_utc

    return MarketDataSnapshot(
        symbol=sym,
        timeframe=str(tf),
        bars=bars,
        last_price=last_price,
        price_ts_utc=ts_dt,
    )


def _build_levels_context(symbol: str) -> dict:
    sym = (symbol or "").strip().upper()
    ctx: dict = {}

    piv = market_data_adapter.get_pivots(sym)
    try:
        piv_map = (piv or {}).get("piv") if isinstance(piv, dict) else None
        if isinstance(piv_map, dict) and piv_map:
            ctx["pivots"] = piv_map
    except Exception:
        pass

    # Optional y_high / y_low: best-effort from daily bars in the local DB.
    try:
        from delivery.discord_bot_head import get_last_n_bars

        rows = get_last_n_bars(sym, tf="1d", n=3)
        if len(rows) >= 2:
            # take the prior bar (yesterday-ish)
            _ts, _o, high, low, _c = rows[-2]
            ctx["y_high"] = float(high)
            ctx["y_low"] = float(low)
    except Exception:
        pass

    # Optional opening range: compute a few common windows from 1m bars.
    # If not available, eval_engine will treat it as missing.
    try:
        from zoneinfo import ZoneInfo

        ET = ZoneInfo("America/New_York")
        from delivery.discord_bot_head import get_last_n_bars

        rows_1m = get_last_n_bars(sym, tf="1m", n=1200)
        if rows_1m:
            last_ts = _parse_iso(str(rows_1m[-1][0]) or "")
            if last_ts is not None:
                day_et = last_ts.astimezone(ET).date()

                for mins in (15, 30, 60):
                    start_et = datetime(day_et.year, day_et.month, day_et.day, 9, 30, tzinfo=ET)
                    end_et = start_et + timedelta(minutes=int(mins))

                    highs: list[float] = []
                    lows: list[float] = []
                    for ts, _o, h, l, _c in rows_1m:
                        dt = _parse_iso(str(ts) or "")
                        if dt is None:
                            continue
                        dt_et = dt.astimezone(ET)
                        if dt_et.date() != day_et:
                            continue
                        if not (start_et <= dt_et < end_et):
                            continue
                        try:
                            highs.append(float(h))
                            lows.append(float(l))
                        except Exception:
                            continue

                    if highs and lows:
                        ctx[f"or_{int(mins)}m"] = {"high": max(highs), "low": min(lows), "date_et": str(day_et)}
    except Exception:
        pass

    return ctx


def _fetch_snaps(symbols: Sequence[str], tf: str, *, lookback: int) -> Mapping[str, MarketDataSnapshot]:
    out: dict[str, MarketDataSnapshot] = {}
    for s in symbols:
        snap = _build_snapshot(s, tf, lookback=lookback)
        if snap is not None:
            out[snap.symbol] = snap
    return out


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="TNT Alerts scheduler daemon (Redis-backed).")
    p.add_argument("--tf", default=(scheduler_redis.TIMEFRAMES[0] if scheduler_redis.TIMEFRAMES else "1m"))
    p.add_argument("--lookback", type=int, default=300)

    args = p.parse_args(argv)

    tf = str(args.tf).strip()
    lookback = int(args.lookback)

    futures_store = None
    if (os.getenv("FUTURES_ENABLED", "0") or "0").strip() == "1":
        try:
            from services.futures.futures_store import FuturesStore  # type: ignore

            futures_store = FuturesStore()
            print("[TNT][ALERTS][SCHED] futures context enabled")
        except Exception as exc:
            futures_store = None
            print(f"[TNT][ALERTS][SCHED] futures context unavailable: {type(exc).__name__}: {exc}")

    queue = (os.getenv("TNT_REDIS_QUEUE", "tnt:jobs") or "tnt:jobs").strip()
    print(f"[TNT][ALERTS][SCHED] tf={tf} lookback={lookback} redis_queue={queue}")

    while True:
        scheduler_redis.sleep_until_boundary(tf)

        def _expand(intent_v1):
            return scheduler_redis.expand_targets_default(intent_v1)

        def _fetch(symbols: Sequence[str], tf2: str):
            return _fetch_snaps(symbols, tf2, lookback=lookback)

        def _gate(_sym: str):
            futures_ctx = None
            if futures_store is not None:
                try:
                    scores = futures_store.get_scores()
                    if scores is not None:
                        reg = str(scores.regime or "").strip().upper()
                        if "BEAR" in reg:
                            es_bias = "BEAR"
                        elif "BULL" in reg:
                            es_bias = "BULL"
                        else:
                            es_bias = "NEUTRAL"
                        futures_ctx = {
                            "es_bias": es_bias,
                            "regime": reg,
                            "vol_mult": float(scores.vol_mult),
                            "updated_utc": int(scores.updated_utc),
                        }
                except Exception:
                    futures_ctx = None
            return GateContext(now_utc=datetime.now(timezone.utc), regime=None, regime_confidence=None, futures=futures_ctx)

        def _levels(sym: str):
            return _build_levels_context(sym)

        def _eval(intent_v1, snap, ctx, gctx, state):
            intent = AlertIntent.model_validate(intent_v1.model_dump(mode="json"))
            return evaluate_alert(intent, snap, ctx, gctx, state)

        scheduler_redis.scheduler_tick(
            tf=tf,
            expand_targets=_expand,
            fetch_snaps=_fetch,
            build_gate_context=_gate,
            build_levels_context=_levels,
            evaluate_alert=_eval,
        )


if __name__ == "__main__":
    raise SystemExit(main())
