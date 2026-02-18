from __future__ import annotations

from datetime import date

from .errors import AlertValidationError
from .models import (
    ActionIncludeChart,
    ActionNotify,
    AlertIntentV1,
    AlertSource,
    AlertTargets,
    ConditionBreak,
    ConditionCross,
    ConditionNewHighLow,
    ConditionRvol,
    ConditionTouch,
    ConfirmSpec,
    CooldownGate,
    DataFreshnessGate,
    ExpiresDate,
    ExpiresDuration,
    ExpiresEOD,
    Gates,
    IndicatorEMA,
    IndicatorSMA,
    IndicatorVWAP,
    LevelNumber,
    LevelRef,
    Lifecycle,
    MarketHoursGate,
    PivotRef,
    RegimeGate,
    SeriesPrice,
)
from .reasons import ERR_UNSUPPORTED_CONDITION


def _series_to_v1(s) -> SeriesPrice | IndicatorVWAP | IndicatorSMA | IndicatorEMA:
    # alert_intent.Series
    if s.type.value == "price":
        return SeriesPrice()

    name = s.name.value if s.name is not None else None
    params = dict(s.params or {})
    if name == "vwap":
        return IndicatorVWAP(params=params)
    if name == "sma":
        return IndicatorSMA(params=params)
    if name == "ema":
        return IndicatorEMA(params=params)
    raise AlertValidationError(ERR_UNSUPPORTED_CONDITION, f"unsupported series indicator: {name!r}")


def _level_to_v1(l) -> PivotRef | LevelRef | LevelNumber:
    if l.type.value == "number":
        return LevelNumber(value=float(l.value))

    if l.type.value == "pivot":
        return PivotRef(name=l.pivot.value)

    if l.type.value == "ref":
        ref = l.ref.value
        minutes = None
        if ref in {"or_high", "or_low"}:
            minutes = int(l.params.get("minutes"))
        return LevelRef(name=ref, minutes=minutes)

    raise AlertValidationError(ERR_UNSUPPORTED_CONDITION, f"unsupported level type: {l.type}")


def alert_intent_to_intent_v1(ai) -> AlertIntentV1:
    """Convert tnt_alerts.alert_intent.AlertIntent -> tnt_alerts.models.AlertIntentV1.

    This keeps the existing /alert runtime working while allowing an LLM to emit
    the stricter schema contract.
    """

    # Source
    src = AlertSource(user_id=ai.source.user_id, channel_id=ai.source.channel_id, request_text=ai.source.request_text)

    # Targets
    if ai.targets.type.value == "symbols":
        targets = AlertTargets(type="symbols", symbols=list(ai.targets.symbols), watchlist=None, max_symbols=ai.targets.max_symbols)
    else:
        targets = AlertTargets(type="watchlist", symbols=[], watchlist=ai.targets.watchlist, max_symbols=ai.targets.max_symbols)

    # Gates
    mh = None
    if ai.gates.market_hours is not None:
        if ai.gates.market_hours.session.value in {"RTH", "ETH"}:
            mh = MarketHoursGate(session=ai.gates.market_hours.session.value, time_window_et=None)
        else:
            w = ai.gates.market_hours.time_window_et
            mh = MarketHoursGate(session=None, time_window_et=(w[0], w[1]) if w and len(w) == 2 else None)

    rg = None
    conf_min = None
    if ai.gates.regime is not None:
        rg = RegimeGate(allowed=[x.value for x in ai.gates.regime.allowed])
        conf_min = ai.gates.regime.min_confidence

    gates = Gates(
        regime=rg,
        confidence_min=conf_min,
        market_hours=mh,
        cooldown=CooldownGate(seconds=ai.gates.cooldown.seconds) if ai.gates.cooldown is not None else None,
        max_triggers=ai.gates.max_triggers.count if ai.gates.max_triggers is not None else 3,
        data_freshness=DataFreshnessGate(price_age_seconds=ai.gates.data_freshness.price_age_seconds) if ai.gates.data_freshness is not None else None,
    )

    # Lifecycle
    expires = ExpiresEOD()
    if ai.lifecycle.expires is not None:
        if ai.lifecycle.expires.type == "relative":
            expires = ExpiresDuration(minutes=ai.lifecycle.expires.minutes, hours=ai.lifecycle.expires.hours, days=ai.lifecycle.expires.days)
        else:
            expires = ExpiresDate(date=ai.lifecycle.expires.date.isoformat())

    lifecycle = Lifecycle(start="now", expires=expires)

    # Condition
    c = ai.condition
    tf = c.timeframe.value
    confirm = ConfirmSpec(mode=c.confirm.value)

    if c.type == "cross":
        cond = ConditionCross(
            op=c.op.value,
            left=_series_to_v1(c.left),
            right=_series_to_v1(c.right),
            timeframe=tf,
            confirm=confirm,
        )
    elif c.type == "touch":
        cond = ConditionTouch(
            left=_series_to_v1(c.left),
            right=_level_to_v1(c.right),
            timeframe=tf,
            confirm=ConfirmSpec(mode="intrabar" if c.confirm.value == "intrabar" else "close"),
        )
    elif c.type == "break":
        cond = ConditionBreak(
            op=c.op.value,
            level=_level_to_v1(c.level),
            timeframe=tf,
            confirm=confirm,
        )
    elif c.type == "new_extreme":
        if tf != "1D":
            raise AlertValidationError(ERR_UNSUPPORTED_CONDITION, "new_extreme only supported on 1D in v1")
        op = "new_high" if c.kind.value == "new_high" else "new_low"
        cond = ConditionNewHighLow(op=op, lookback_days=int(c.lookback_bars), timeframe="1D")
    elif c.type == "rvol":
        cond = ConditionRvol(lookback_bars=int(c.lookback_bars), threshold=float(c.threshold), timeframe=tf)
    else:
        raise AlertValidationError(ERR_UNSUPPORTED_CONDITION, f"unsupported condition type: {getattr(c, 'type', None)!r}")

    # Actions
    actions = []
    for a in ai.actions:
        if a.type == "discord_notify":
            actions.append(ActionNotify(style=a.style.value))
        elif a.type == "include_chart":
            actions.append(ActionIncludeChart(chart=a.chart.value))
        else:
            raise AlertValidationError(ERR_UNSUPPORTED_CONDITION, f"unsupported action type: {a.type!r}")

    return AlertIntentV1(
        version="1.0",
        source=src,
        targets=targets,
        condition=cond,
        gates=gates,
        lifecycle=lifecycle,
        actions=actions or [ActionNotify()],
    )
