from __future__ import annotations

import json

from .models import (
    ActionIncludeChart,
    ActionNotify,
    AlertIntentV1,
    ConfirmSpec,
    ConditionBreak,
    ConditionCross,
    ConditionNewHighLow,
    ConditionRvol,
    ConditionTouch,
    ExpiresDate,
    ExpiresDuration,
    ExpiresEOD,
    IndicatorEMA,
    IndicatorSMA,
    IndicatorVWAP,
    LevelNumber,
    LevelRef,
    PivotRef,
    SeriesPrice,
)


def _series_to_dsl(series) -> str:
    if isinstance(series, SeriesPrice):
        return "price"
    if isinstance(series, IndicatorVWAP):
        return "vwap"
    if isinstance(series, IndicatorSMA):
        n = int(series.params.get("n") or 0)
        return f"sma({n})"
    if isinstance(series, IndicatorEMA):
        n = int(series.params.get("n") or 0)
        return f"ema({n})"
    name = getattr(series, "name", "?")
    return str(name)


def _level_to_dsl(level) -> str:
    if isinstance(level, LevelNumber):
        return f"{float(level.value):g}"
    if isinstance(level, PivotRef):
        return f"pivot({level.name})"
    if isinstance(level, LevelRef):
        if level.name in {"y_high", "y_low"}:
            return level.name
        mins = int(level.minutes or 0)
        return f"{level.name}({mins})"
    name = getattr(level, "name", "?")
    return str(name)


def _confirm_to_dsl(confirm: ConfirmSpec | None) -> str:
    if not confirm:
        return ""
    if confirm.mode == "n_closes":
        return f" CONFIRM n_closes({int(confirm.n or 1)})"
    return f" CONFIRM {confirm.mode}"


def _until_to_dsl(expires) -> str:
    if isinstance(expires, ExpiresEOD):
        return "EOD"
    if isinstance(expires, ExpiresDate):
        return expires.date
    if isinstance(expires, ExpiresDuration):
        if expires.minutes is not None:
            return f"{int(expires.minutes)}m"
        if expires.hours is not None:
            return f"{int(expires.hours)}h"
        if expires.days is not None:
            return f"{int(expires.days)}d"
    return "EOD"


def intent_to_dsl(intent: AlertIntentV1) -> str:
    if intent.targets.type == "watchlist":
        targets = f"watchlist:{(intent.targets.watchlist or '').strip()}".strip()
    else:
        targets = ",".join([s.strip().upper() for s in intent.targets.symbols if s and s.strip()])

    cond = intent.condition
    when = ""
    if isinstance(cond, ConditionCross):
        when = f"{cond.op}({_series_to_dsl(cond.left)},{_series_to_dsl(cond.right)})" + _confirm_to_dsl(cond.confirm)
    elif isinstance(cond, ConditionTouch):
        when = f"touches({_series_to_dsl(cond.left)},{_level_to_dsl(cond.right)})" + _confirm_to_dsl(cond.confirm)
    elif isinstance(cond, ConditionBreak):
        when = f"{cond.op}({_level_to_dsl(cond.level)})" + _confirm_to_dsl(cond.confirm)
    elif isinstance(cond, ConditionNewHighLow):
        when = f"{cond.op}({int(cond.lookback_days)}d)"
    elif isinstance(cond, ConditionRvol):
        when = f"rvol({int(cond.lookback_bars)}) > {float(cond.threshold):g}"
    else:
        when = "(unsupported)"

    tf = getattr(cond, "timeframe", None) or "5m"

    gates = []
    g = intent.gates
    if g.regime and g.regime.allowed:
        regimes = ",".join(g.regime.allowed)
        gates.append(f"regime IN {{{regimes}}}")
    if g.confidence_min is not None:
        gates.append(f"confidence >= {float(g.confidence_min):.2f}")
    if g.cooldown is not None:
        sec = int(g.cooldown.seconds)
        if sec % 3600 == 0 and sec >= 3600:
            gates.append(f"cooldown {int(sec / 3600)}h")
        elif sec % 60 == 0 and sec >= 60:
            gates.append(f"cooldown {int(sec / 60)}m")
        else:
            gates.append(f"cooldown {sec}s")
    if g.max_triggers:
        gates.append(f"max_triggers {int(g.max_triggers)}")
    if g.data_freshness is not None:
        gates.append(f"price_age <= {int(g.data_freshness.price_age_seconds)}s")

    if_clause = f" IF {' AND '.join(gates)}" if gates else ""

    during = ""
    mh = g.market_hours
    if mh:
        if mh.time_window_et is not None:
            start, end = mh.time_window_et
            during = f" DURING {start}-{end} ET"
        elif mh.session is not None:
            during = f" DURING {mh.session}"

    until = f" UNTIL {_until_to_dsl(intent.lifecycle.expires)}"

    actions = []
    for a in intent.actions:
        if isinstance(a, ActionNotify):
            actions.append(f"notify({a.style})")
        elif isinstance(a, ActionIncludeChart):
            actions.append(f"include_chart({a.chart})")
        else:
            actions.append("notify(compact)")
    then = f" THEN {' AND '.join(actions)}" if actions else ""

    return f"{targets} WHEN {when} ON {tf}{if_clause}{during}{until}{then}".strip()


def intent_to_pretty_json(intent: AlertIntentV1) -> str:
    return json.dumps(intent.model_dump(mode="json"), indent=2, ensure_ascii=False)
