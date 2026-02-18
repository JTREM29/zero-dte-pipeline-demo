from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Optional, Sequence

from .alert_intent import (
    AlertIntent,
    BreakCondition,
    BreakOp,
    Condition,
    ConfirmMode,
    CrossCondition,
    CrossOp,
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
    SUPPRESS_COOLDOWN,
    SUPPRESS_CONFIDENCE,
    SUPPRESS_DATA_MISSING,
    SUPPRESS_DATA_STALE,
    SUPPRESS_INDICATOR_UNAVAILABLE,
    SUPPRESS_MAX_TRIGGERS_REACHED,
    SUPPRESS_REGIME,
    SUPPRESS_SESSION_MISMATCH,
    TRIGGERED_CONDITION_TRUE,
)


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


def resolve_series(series: Series, snap: MarketDataSnapshot, *, use_close: bool) -> Optional[float]:
    if series.type == SeriesType.price:
        if use_close:
            return snap.bars[-1].c if snap.bars else None
        return snap.last_price

    closes = [b.c for b in snap.bars]
    name = series.name
    if name is None:
        return None

    if name.value == "vwap":
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

    prev_left = resolve_series(cond.left, prev_snap, use_close=True)
    prev_right = resolve_series(cond.right, prev_snap, use_close=True)
    curr_left = resolve_series(cond.left, snap, use_close=True)
    curr_right = resolve_series(cond.right, snap, use_close=True)

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


def gates_pass(intent: AlertIntent, snap: MarketDataSnapshot, gctx: GateContext, state: dict) -> tuple[bool, list[dict]]:
    reasons: list[dict] = []

    if intent.gates.data_freshness:
        max_age = intent.gates.data_freshness.price_age_seconds
        age = (gctx.now_utc - snap.price_ts_utc).total_seconds()
        if age > max_age:
            reasons.append({"code": SUPPRESS_DATA_STALE, "age_sec": age, "max_age_sec": max_age})
            return False, reasons

    if intent.gates.market_hours:
        mh = intent.gates.market_hours
        if mh.session == Session.RTH:
            if not is_in_rth(gctx.now_utc):
                reasons.append({"code": SUPPRESS_SESSION_MISMATCH, "session": "RTH"})
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

    if intent.gates.cooldown and intent.gates.cooldown.seconds > 0:
        last_ts = state.get("last_trigger_ts_utc")
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

    ok, gate_reasons = gates_pass(intent, snap, gctx, state)
    if not ok:
        event["decision"] = "SUPPRESSED"
        event["reason_codes"] = [r["code"] for r in gate_reasons]
        event["eval"]["gates"] = gate_reasons
        return event

    triggered, eval_details = eval_condition(intent, snap, context)
    event["eval"]["condition"] = eval_details

    if not triggered:
        event["decision"] = "NO_TRIGGER"
        event["reason_codes"] = [NO_TRIGGER_CONDITION_FALSE]
        return event

    state["last_trigger_ts_utc"] = gctx.now_utc
    state["trigger_count"] = int(state.get("trigger_count", 0)) + 1

    event["decision"] = "TRIGGERED"
    event["reason_codes"] = [TRIGGERED_CONDITION_TRUE]
    return event


# --- stubs you implement using your market calendar ---


def is_in_rth(now_utc: datetime) -> bool:
    return True


def is_in_custom_window_et(now_utc: datetime, window_et: list[str]) -> bool:
    return True
