from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time, timezone
from typing import Any, Optional, Sequence

from .alert_intent import (
    AlertIntent,
    BreakCondition,
    BreakOp,
    Condition,
    ConfirmMode,
    CrossCondition,
    CrossOp,
    MarketHoursGate,
    Level,
    LevelRefName,
    LevelType,
    NewExtremeCondition,
    NewExtremeKind,
    Regime,
    RVOLCondition,
    Series,
    SeriesType,
    Session,
    TouchCondition,
)
from .reasons import (
    NO_TRIGGER_CONDITION_FALSE,
    SKIP_EARNINGS_WINDOW,
    SKIP_CTX_MISSING_EARNINGS,
    SKIP_CTX_MISSING_NEWS,
    SKIP_MACRO_WINDOW,
    SKIP_MARKET_NEWS_RECENT,
    SKIP_NEWS_RECENT,
    SUPPRESS_COOLDOWN,
    SUPPRESS_CONFIDENCE,
    SUPPRESS_DATA_MISSING,
    SUPPRESS_DATA_STALE,
    SUPPRESS_INDICATOR_UNAVAILABLE,
    SUPPRESS_MAX_TRIGGERS_REACHED,
    SUPPRESS_FUTURES_CONFLICT_BULLISH,
    SUPPRESS_FUTURES_CONFLICT_BEARISH,
    SUPPRESS_REGIME,
    SUPPRESS_SESSION_MISMATCH,
    TRIGGERED_CONDITION_TRUE,
    TRIGGERED_DEBUG_FORCE,
)

from .direction import infer_direction

from .gates.earnings_blackout import earnings_blackout
from .gates.macro_blackout import macro_blackout
from .gates.news_blackout import news_blackout


# NOTE:
# This module is intentionally deterministic and pure.
# It does not fetch data or talk to Redis/Discord. It just evaluates.


@dataclass(frozen=True)
class Bar:
    ts_utc: datetime
    o: float
    h: float
    l: float
    c: float
    v: float


@dataclass(frozen=True)
class MarketDataSnapshot:
    symbol: str
    timeframe: str
    bars: Sequence[Bar]
    last_price: float
    price_ts_utc: datetime


def compute_sma(closes: list[float], n: int) -> Optional[float]:
    if len(closes) < n:
        return None
    return sum(closes[-n:]) / n


def compute_ema(closes: list[float], n: int) -> Optional[float]:
    if len(closes) < n:
        return None
    k = 2 / (n + 1)
    ema = sum(closes[:n]) / n
    for x in closes[n:]:
        ema = (x * k) + (ema * (1 - k))
    return ema


def compute_vwap(bars: Sequence[Bar]) -> Optional[float]:
    pv = 0.0
    vv = 0.0
    for b in bars:
        typical = (b.h + b.l + b.c) / 3.0
        pv += typical * b.v
        vv += b.v
    if vv <= 0:
        return None
    return pv / vv


def _filter_bars_for_vwap(bars: Sequence[Bar], *, anchor: str) -> Sequence[Bar]:
    """Optionally filter bars for VWAP calculation.

    Anchors:
    - "DAY": bars from the current ET trading date
    - "RTH": bars from current ET date with t >= 09:30 ET
    """

    if not bars:
        return bars

    try:
        from zoneinfo import ZoneInfo

        et_tz = ZoneInfo("America/New_York")
    except Exception:
        return bars

    last_et = bars[-1].ts_utc.astimezone(et_tz)
    session_date = last_et.date()

    anchor_norm = str(anchor or "").strip().upper() or "DAY"
    rth_open = time(9, 30)

    out: list[Bar] = []
    for b in bars:
        bet = b.ts_utc.astimezone(et_tz)
        if bet.date() != session_date:
            continue
        if anchor_norm == "RTH" and bet.time() < rth_open:
            continue
        out.append(b)
    return out


def resolve_series(series: Series, snap: MarketDataSnapshot, context: dict | None = None, *, use_close: bool) -> Optional[float]:
    if series.type == SeriesType.price:
        if use_close:
            return snap.bars[-1].c if snap.bars else None
        return snap.last_price

    closes = [b.c for b in snap.bars]
    name = series.name
    if name is None:
        return None

    if name.value == "vwap":
        anchor = None
        if isinstance(context, dict):
            anchor = context.get("vwap_anchor") or context.get("vwap_session")
        if anchor:
            return compute_vwap(_filter_bars_for_vwap(snap.bars, anchor=str(anchor)))
        return compute_vwap(snap.bars)
    if name.value == "sma":
        n = int(series.params.get("n", 200))
        return compute_sma(closes, n)
    if name.value == "ema":
        n = int(series.params.get("n", 20))
        return compute_ema(closes, n)
    return None


def resolve_level(level: Level, snap: MarketDataSnapshot, context: dict) -> Optional[float]:
    if level.type == LevelType.number:
        return None if level.value is None else float(level.value)

    if level.type == LevelType.pivot:
        pivots = context.get("pivots")
        if pivots is None or level.pivot is None:
            return None
        return float(pivots[level.pivot.value])

    if level.type == LevelType.ref:
        if level.ref is None:
            return None
        ref = level.ref
        if ref == LevelRefName.y_high:
            return float(context["y_high"])
        if ref == LevelRefName.y_low:
            return float(context["y_low"])
        if ref in (LevelRefName.or_high, LevelRefName.or_low):
            mins = int(level.params["minutes"])
            orv = context.get(f"or_{mins}m")
            if not orv:
                return None
            return float(orv["high"] if ref == LevelRefName.or_high else orv["low"])

    return None


def eval_cross(cond: CrossCondition, snap: MarketDataSnapshot, context: dict) -> tuple[bool, dict]:
    if len(snap.bars) < 2:
        return False, {"reason": SUPPRESS_DATA_MISSING}

    prev_bar = snap.bars[-2]
    prev_snap = MarketDataSnapshot(
        symbol=snap.symbol,
        timeframe=snap.timeframe,
        bars=snap.bars[:-1],
        last_price=prev_bar.c,
        price_ts_utc=prev_bar.ts_utc,
    )

    prev_left = resolve_series(cond.left, prev_snap, context, use_close=True)
    prev_right = resolve_series(cond.right, prev_snap, context, use_close=True)
    curr_left = resolve_series(cond.left, snap, context, use_close=True)
    curr_right = resolve_series(cond.right, snap, context, use_close=True)

    if prev_left is None or prev_right is None or curr_left is None or curr_right is None:
        return False, {"reason": SUPPRESS_INDICATOR_UNAVAILABLE}

    crossed = False
    if cond.op == CrossOp.crosses_above:
        crossed = (prev_left <= prev_right) and (curr_left > curr_right)
    elif cond.op == CrossOp.crosses_below:
        crossed = (prev_left >= prev_right) and (curr_left < curr_right)

    return crossed, {
        "prev_left": prev_left,
        "prev_right": prev_right,
        "curr_left": curr_left,
        "curr_right": curr_right,
        "op": cond.op.value,
    }


def eval_touch(cond: TouchCondition, snap: MarketDataSnapshot, context: dict) -> tuple[bool, dict]:
    if not snap.bars:
        return False, {"reason": SUPPRESS_DATA_MISSING}

    lvl = resolve_level(cond.right, snap, context)
    if lvl is None:
        return False, {"reason": SUPPRESS_INDICATOR_UNAVAILABLE}

    bar = snap.bars[-1]
    if cond.confirm == ConfirmMode.intrabar:
        touched = bar.l <= lvl <= bar.h
        return touched, {"level": lvl, "bar_low": bar.l, "bar_high": bar.h}

    touched = bar.c == lvl
    return touched, {"level": lvl, "bar_close": bar.c}


def eval_break(cond: BreakCondition, snap: MarketDataSnapshot, context: dict) -> tuple[bool, dict]:
    if len(snap.bars) < 2:
        return False, {"reason": SUPPRESS_DATA_MISSING}

    lvl = resolve_level(cond.level, snap, context)
    if lvl is None:
        return False, {"reason": SUPPRESS_INDICATOR_UNAVAILABLE}

    prev = snap.bars[-2]
    curr = snap.bars[-1]

    if cond.confirm == ConfirmMode.intrabar:
        if cond.op == BreakOp.breaks_above:
            return (curr.h > lvl and prev.h <= lvl), {"level": lvl, "prev_high": prev.h, "curr_high": curr.h}
        return (curr.l < lvl and prev.l >= lvl), {"level": lvl, "prev_low": prev.l, "curr_low": curr.l}

    if cond.op == BreakOp.breaks_above:
        return (prev.c <= lvl and curr.c > lvl), {"level": lvl, "prev_close": prev.c, "curr_close": curr.c}
    return (prev.c >= lvl and curr.c < lvl), {"level": lvl, "prev_close": prev.c, "curr_close": curr.c}


def eval_new_extreme(cond: NewExtremeCondition, snap: MarketDataSnapshot, context: dict) -> tuple[bool, dict]:
    if len(snap.bars) < cond.lookback_bars:
        return False, {"reason": SUPPRESS_DATA_MISSING}

    window = snap.bars[-cond.lookback_bars :]
    curr = window[-1]

    highs = [b.h for b in window[:-1]]
    lows = [b.l for b in window[:-1]]

    if cond.kind == NewExtremeKind.new_high:
        thr = max(highs) if highs else None
        if thr is None:
            return False, {"reason": SUPPRESS_DATA_MISSING}
        return (curr.h > thr), {"lookback": cond.lookback_bars, "prior_max_high": thr, "curr_high": curr.h}

    thr = min(lows) if lows else None
    if thr is None:
        return False, {"reason": SUPPRESS_DATA_MISSING}
    return (curr.l < thr), {"lookback": cond.lookback_bars, "prior_min_low": thr, "curr_low": curr.l}


def eval_rvol(cond: RVOLCondition, snap: MarketDataSnapshot, context: dict) -> tuple[bool, dict]:
    n = cond.lookback_bars
    if len(snap.bars) < n + 1:
        return False, {"reason": SUPPRESS_DATA_MISSING}

    curr = snap.bars[-1]
    prior = snap.bars[-(n + 1) : -1]
    avg = sum(b.v for b in prior) / max(1, len(prior))
    if avg <= 0:
        return False, {"reason": SUPPRESS_DATA_MISSING}

    rvol = curr.v / avg
    return (rvol > cond.threshold), {"rvol": rvol, "threshold": cond.threshold, "curr_v": curr.v, "avg_v": avg}


@dataclass
class GateContext:
    now_utc: datetime
    regime: Optional[Regime] = None
    regime_confidence: Optional[float] = None
    # Optional services injected by the scheduler.
    calendar: Any | None = None
    news: Any | None = None
    futures: Any | None = None


def gates_pass(intent: AlertIntent, snap: MarketDataSnapshot, gctx: GateContext, state: dict) -> tuple[bool, list[dict]]:
    reasons: list[dict] = []

    def _earnings_near_note_if_fresh(symbol: str) -> str | None:
        """Return a compact earnings note (today/tomorrow only) if cache is fresh.

        This is for secondary notes (e.g., futures conflict) and must not call external APIs.
        """

        if gctx.calendar is None:
            return None

        # Preferred: canonical ctx snapshot (aligns wording across preview/gates).
        try:
            if hasattr(gctx.calendar, "get_symbol_context_snapshot"):
                ctx = gctx.calendar.get_symbol_context_snapshot(symbol)
                if isinstance(ctx, dict):
                    e = ctx.get("earnings")
                    if isinstance(e, dict) and bool(e.get("fresh")):
                        note = str(e.get("note") or "").strip()
                        return note or None
        except Exception:
            pass

        # Strict cutover behavior.
        try:
            from services.context.ctx_mode import ctx_enabled, ctx_strict_mode
            from services.context.context_miss import record_ctx_miss

            if ctx_enabled():
                mode = ctx_strict_mode()
                r = getattr(gctx.calendar, "r", None)
                record_ctx_miss(r, "futures_conflict_gate", sym=str(symbol or "").strip().upper(), why="ctx_missing")
                if mode == "on":
                    # Strict: do not attach secondary earnings notes if ctx is missing.
                    return None
        except Exception:
            pass

        # Fallback (temporary): legacy cached earnings blob.
        try:
            ev = gctx.calendar.get_cached_earnings(symbol)
        except Exception:
            return None
        if not isinstance(ev, dict):
            return None

        refreshed_utc = str(ev.get("refreshed_utc") or "").strip()
        if not refreshed_utc:
            return None
        try:
            dt_ref = datetime.fromisoformat(refreshed_utc.replace("Z", "+00:00"))
            if dt_ref.tzinfo is None:
                dt_ref = dt_ref.replace(tzinfo=timezone.utc)
            dt_ref = dt_ref.astimezone(timezone.utc)
        except Exception:
            return None

        try:
            stale_hours = int(__import__("os").getenv("EARNINGS_PREVIEW_STALE_HOURS", "48"))
        except Exception:
            stale_hours = 48
        stale_hours = max(1, min(168, int(stale_hours)))

        age_s = int((datetime.now(timezone.utc) - dt_ref).total_seconds())
        if age_s < 0:
            age_s = 0
        if age_s > (stale_hours * 3600):
            return None

        try:
            from services.calendar.earnings_overlay import build_earnings_near_note

            return build_earnings_near_note(ev)
        except Exception:
            return None

    def _parse_state_dt(x: object) -> datetime | None:
        if x is None:
            return None
        if isinstance(x, datetime):
            return x
        if isinstance(x, str):
            s = x.strip()
            if not s:
                return None
            try:
                raw = s.replace("Z", "+00:00")
                dt = datetime.fromisoformat(raw)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                return dt
            except Exception:
                return None
        return None

    if intent.gates.data_freshness:
        max_age = intent.gates.data_freshness.price_age_seconds
        age = (gctx.now_utc - snap.price_ts_utc).total_seconds()
        if age > max_age:
            reasons.append({"code": SUPPRESS_DATA_STALE, "age_sec": age, "max_age_sec": max_age})
            return False, reasons

    # RTH-default: if market_hours gate is missing, treat it as RTH.
    mh = intent.gates.market_hours if intent.gates is not None else None
    if mh is None:
        try:
            mh = MarketHoursGate(session=Session.RTH, time_window_et=None)
        except Exception:
            mh = None

    if mh is not None:
        if mh.session == Session.RTH:
            if not is_in_rth(gctx.now_utc):
                reasons.append({"code": SUPPRESS_SESSION_MISMATCH, "session": "RTH"})
                return False, reasons
        elif mh.session == Session.ETH:
            if not is_in_eth(gctx.now_utc):
                reasons.append({"code": SUPPRESS_SESSION_MISMATCH, "session": "ETH"})
                return False, reasons
        elif mh.session == Session.CUSTOM:
            if not mh.time_window_et or not is_in_custom_window_et(gctx.now_utc, mh.time_window_et):
                reasons.append({"code": SUPPRESS_SESSION_MISMATCH, "session": "CUSTOM", "window": mh.time_window_et})
                return False, reasons

    if intent.gates.regime:
        rg = intent.gates.regime
        if gctx.regime is None:
            reasons.append({"code": SUPPRESS_DATA_MISSING, "missing": "regime"})
            return False, reasons
        if gctx.regime not in rg.allowed:
            reasons.append({"code": SUPPRESS_REGIME, "regime": gctx.regime.value})
            return False, reasons
        if rg.min_confidence is not None:
            if gctx.regime_confidence is None or gctx.regime_confidence < rg.min_confidence:
                reasons.append({"code": SUPPRESS_CONFIDENCE, "conf": gctx.regime_confidence, "min": rg.min_confidence})
                return False, reasons

    # Calendar/news blackout gates (optional; injected via GateContext).
    if intent.gates.macro_blackout and gctx.calendar is not None:
        mg = intent.gates.macro_blackout
        res = macro_blackout(
            gctx.calendar,
            now_utc=gctx.now_utc,
            event_types=mg.event_types,
            pre_minutes=mg.pre_minutes,
            post_minutes=mg.post_minutes,
        )
        if not res.ok:
            reasons.append({"code": res.code or SKIP_MACRO_WINDOW, "note": res.note, **(res.details or {})})
            return False, reasons

    if intent.gates.earnings_blackout and gctx.calendar is not None:
        eg = intent.gates.earnings_blackout
        res = earnings_blackout(
            snap.symbol,
            gctx.calendar,
            now_utc=gctx.now_utc,
            pre_minutes=eg.pre_minutes,
            post_minutes=eg.post_minutes,
            confirmed_only=bool(eg.confirmed_only),
        )
        if not res.ok:
            reasons.append({"code": res.code or SKIP_EARNINGS_WINDOW, "note": res.note, **(res.details or {})})
            return False, reasons

    if intent.gates.news_blackout and gctx.news is not None:
        ng = intent.gates.news_blackout
        res = news_blackout(
            snap.symbol,
            gctx.news,
            now_utc=gctx.now_utc,
            symbol_minutes=ng.minutes,
            market_minutes=ng.market_minutes,
        )
        if not res.ok:
            # Preserve explicit code choice from gate.
            fallback = SKIP_NEWS_RECENT
            if (res.code or "") == SKIP_MARKET_NEWS_RECENT:
                fallback = SKIP_MARKET_NEWS_RECENT
            reasons.append({"code": res.code or fallback, "note": res.note, **(res.details or {})})
            return False, reasons

    # Futures direction-aware suppression (opt-in; does not break existing alerts).
    # Enable globally via TNT_ALERTS_FUTURES_GATE=1 or per-alert via tags.futures_gate.
    try:
        tag_val = intent.tags.get("futures_gate") if isinstance(intent.tags, dict) else None
        tag_s = str(tag_val).strip().lower() if tag_val is not None else ""
        tag_enabled = tag_s in {"1", "true", "yes", "on", "enabled"}
        tag_disabled = tag_s in {"0", "false", "no", "off", "disabled"}
    except Exception:
        tag_enabled = False
        tag_disabled = False

    env_enabled = (str(__import__("os").getenv("TNT_ALERTS_FUTURES_GATE", "0") or "0").strip() == "1")
    enabled = (tag_enabled or env_enabled) and not tag_disabled

    fut = gctx.futures if enabled else None
    if enabled and isinstance(fut, dict):
        es_bias = str(fut.get("es_bias") or "").strip().upper()
        if es_bias in {"BULL", "BEAR"}:
            d = infer_direction(intent)
            if d == "BULLISH" and es_bias == "BEAR":
                r = {"code": SUPPRESS_FUTURES_CONFLICT_BULLISH, "es_bias": es_bias, "note": "Futures bearish vs BULLISH intent"}
                try:
                    en = _earnings_near_note_if_fresh(snap.symbol)
                    if en:
                        r["earnings_note"] = en
                except Exception:
                    pass
                reasons.append(r)
                return False, reasons
            if d == "BEARISH" and es_bias == "BULL":
                r = {"code": SUPPRESS_FUTURES_CONFLICT_BEARISH, "es_bias": es_bias, "note": "Futures bullish vs BEARISH intent"}
                try:
                    en = _earnings_near_note_if_fresh(snap.symbol)
                    if en:
                        r["earnings_note"] = en
                except Exception:
                    pass
                reasons.append(r)
                return False, reasons

    if intent.gates.cooldown and intent.gates.cooldown.seconds > 0:
        last_ts = _parse_state_dt(state.get("last_trigger_ts_utc"))
        if last_ts is not None:
            dt = (gctx.now_utc - last_ts).total_seconds()
            if dt < intent.gates.cooldown.seconds:
                reasons.append({"code": SUPPRESS_COOLDOWN, "since_sec": dt, "cooldown_sec": intent.gates.cooldown.seconds})
                return False, reasons

    if intent.gates.max_triggers:
        cnt = int(state.get("trigger_count", 0))
        if cnt >= intent.gates.max_triggers.count:
            reasons.append({"code": SUPPRESS_MAX_TRIGGERS_REACHED, "count": cnt, "max": intent.gates.max_triggers.count})
            return False, reasons

    return True, reasons


def eval_condition(intent: AlertIntent, snap: MarketDataSnapshot, context: dict) -> tuple[bool, dict]:
    c: Condition = intent.condition
    if isinstance(c, CrossCondition):
        return eval_cross(c, snap, context)
    if isinstance(c, TouchCondition):
        return eval_touch(c, snap, context)
    if isinstance(c, BreakCondition):
        return eval_break(c, snap, context)
    if isinstance(c, NewExtremeCondition):
        return eval_new_extreme(c, snap, context)
    if isinstance(c, RVOLCondition):
        return eval_rvol(c, snap, context)
    return False, {"reason": "ERR_UNSUPPORTED_CONDITION"}


def evaluate_alert(intent: AlertIntent, snap: MarketDataSnapshot, context: dict, gctx: GateContext, state: dict) -> dict:
    event = {
        "ts_utc": gctx.now_utc.isoformat(),
        "symbol": snap.symbol,
        "intent_version": intent.version,
        "decision": None,
        "reason_codes": [],
        "eval": {},
        "data_age_sec": (gctx.now_utc - snap.price_ts_utc).total_seconds(),
    }

    def _record_last_eval(*, ok: bool, skipped: bool, reasons: list[dict] | None = None) -> None:
        try:
            state["last_eval"] = {
                "ts_utc": str(event.get("ts_utc") or ""),
                "ok": bool(ok),
                "skipped": bool(skipped),
                "decision": str(event.get("decision") or ""),
                "reasons": list(reasons or []),
            }
        except Exception:
            return

    ok, gate_reasons = gates_pass(intent, snap, gctx, state)
    if not ok:
        event["decision"] = "SUPPRESSED"
        event["reason_codes"] = [r["code"] for r in gate_reasons]
        event["eval"]["gates"] = gate_reasons
        _record_last_eval(ok=False, skipped=True, reasons=gate_reasons)
        return event

    triggered, eval_details = eval_condition(intent, snap, context)
    event["eval"]["condition"] = eval_details

    # Dev-only: deterministic trigger mode to validate trigger -> queue -> Discord.
    # When enabled, VWAP-cross intents act like "price vs VWAP" so we don't have
    # to wait for a true crossing. All gates (session/cooldown/max_triggers/etc)
    # are still enforced by gates_pass() above.
    try:
        import os

        debug_force = (str(os.getenv("TNT_ALERTS_DEBUG_FORCE_TRIGGER", "0") or "0").strip().lower() in {"1", "true", "yes", "on"})
    except Exception:
        debug_force = False

    if debug_force and not triggered:
        try:
            if isinstance(intent.condition, CrossCondition):
                op = getattr(intent.condition, "op", None)
                curr_left = eval_details.get("curr_left") if isinstance(eval_details, dict) else None
                curr_right = eval_details.get("curr_right") if isinstance(eval_details, dict) else None

                if curr_left is not None and curr_right is not None:
                    # "price vs VWAP" proxy.
                    if op == CrossOp.crosses_above and float(curr_left) > float(curr_right):
                        triggered = True
                    elif op == CrossOp.crosses_below and float(curr_left) < float(curr_right):
                        triggered = True
                else:
                    # If VWAP is missing, still allow a single forced trigger to
                    # validate the delivery plumbing.
                    if not bool(state.get("debug_force_triggered")):
                        triggered = True

                if triggered:
                    try:
                        state["debug_force_triggered"] = True
                    except Exception:
                        pass
                    try:
                        if isinstance(eval_details, dict):
                            eval_details["debug_forced"] = True
                    except Exception:
                        pass
        except Exception:
            pass

    if not triggered:
        event["decision"] = "NO_TRIGGER"
        event["reason_codes"] = [NO_TRIGGER_CONDITION_FALSE]
        _record_last_eval(ok=True, skipped=False, reasons=[{"code": NO_TRIGGER_CONDITION_FALSE}])
        return event

    # Persist as ISO-8601 string so Redis JSON state remains serializable.
    state["last_trigger_ts_utc"] = gctx.now_utc.isoformat()
    state["trigger_count"] = int(state.get("trigger_count", 0)) + 1

    event["decision"] = "TRIGGERED"
    if bool((event.get("eval") or {}).get("condition", {}).get("debug_forced")):
        event["reason_codes"] = [TRIGGERED_DEBUG_FORCE]
        _record_last_eval(ok=True, skipped=False, reasons=[{"code": TRIGGERED_DEBUG_FORCE}])
    else:
        event["reason_codes"] = [TRIGGERED_CONDITION_TRUE]
        _record_last_eval(ok=True, skipped=False, reasons=[{"code": TRIGGERED_CONDITION_TRUE}])
    return event


# --- stubs you implement using your market calendar ---


def is_in_rth(now_utc: datetime) -> bool:
    try:
        from zoneinfo import ZoneInfo

        et = now_utc.astimezone(ZoneInfo("America/New_York"))
    except Exception:
        # Conservative fallback: if we can't convert, do not allow triggers.
        return False

    # Mon-Fri only (holiday calendar handled by separate macro/earnings gates).
    if et.weekday() >= 5:
        return False

    # RTH = 09:30:00 <= t < 16:00:00 ET
    mins = et.hour * 60 + et.minute
    start = 9 * 60 + 30
    end = 16 * 60
    if mins < start:
        return False
    if mins > end:
        return False
    if mins == end:
        # If exactly 16:00, treat as out of session.
        return False
    return True


def is_in_eth(now_utc: datetime) -> bool:
    try:
        from zoneinfo import ZoneInfo

        et = now_utc.astimezone(ZoneInfo("America/New_York"))
    except Exception:
        return False

    # Mon-Fri only (holiday calendar handled elsewhere).
    if et.weekday() >= 5:
        return False

    # ETH (stocks) ~ 04:00 <= t < 20:00 ET
    mins = et.hour * 60 + et.minute
    start = 4 * 60
    end = 20 * 60
    return start <= mins < end


def is_in_custom_window_et(now_utc: datetime, window_et: list[str]) -> bool:
    if not window_et or len(window_et) != 2:
        return False

    try:
        from zoneinfo import ZoneInfo

        et = now_utc.astimezone(ZoneInfo("America/New_York"))
    except Exception:
        return False

    # Only allow on weekdays by default.
    if et.weekday() >= 5:
        return False

    def _parse_hhmm(s: str) -> int | None:
        try:
            raw = (s or "").strip()
            if not raw:
                return None
            hh, mm = raw.split(":", 1)
            h = int(hh)
            m = int(mm)
            if h < 0 or h > 23 or m < 0 or m > 59:
                return None
            return h * 60 + m
        except Exception:
            return None

    start = _parse_hhmm(str(window_et[0]))
    end = _parse_hhmm(str(window_et[1]))
    if start is None or end is None:
        return False

    now_mins = et.hour * 60 + et.minute

    # Treat end as exclusive (matches RTH behavior).
    if start <= end:
        return start <= now_mins < end

    # Overnight window (e.g., 15:30-09:45)
    return (now_mins >= start) or (now_mins < end)
