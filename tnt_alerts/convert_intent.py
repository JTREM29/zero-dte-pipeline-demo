from __future__ import annotations

from datetime import date, datetime, timezone

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


def _now_utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _series_from_v1(s) -> dict:
    # tnt_alerts.models.Series -> alert_intent.Series (dict form)
    # v1 uses {type:"series", name:"price"} for price.
    st = getattr(s, "type", None)
    stv = getattr(st, "value", None) if hasattr(st, "value") else (str(st).strip() if st is not None else None)
    if stv == "series":
        return {"type": "price", "field": "last", "name": None, "params": {}}

    # v1 indicators already look like {type:"indicator", name:"vwap"|"sma"|"ema", params:{...}}
    name = getattr(s, "name", None)
    namev = getattr(name, "value", None) if hasattr(name, "value") else (str(name).strip() if name is not None else None)
    params = dict(getattr(s, "params", {}) or {})
    if stv == "indicator" and namev in {"vwap", "sma", "ema"}:
        return {"type": "indicator", "name": namev, "params": params}

    raise AlertValidationError(ERR_UNSUPPORTED_CONDITION, f"unsupported series in v1: type={stv!r} name={namev!r}")


def _level_from_v1(l) -> dict:
    lt = getattr(l, "type", None)
    ltv = getattr(lt, "value", None) if hasattr(lt, "value") else (str(lt).strip() if lt is not None else None)

    if ltv == "number":
        return {"type": "number", "value": float(getattr(l, "value"))}

    if ltv == "pivot":
        # v1 uses {type:"pivot", name:"P"|...}
        name = getattr(l, "name", None)
        namev = getattr(name, "value", None) if hasattr(name, "value") else (str(name).strip() if name is not None else None)
        if not namev:
            raise AlertValidationError(ERR_UNSUPPORTED_CONDITION, "pivot level missing name")
        return {"type": "pivot", "pivot": str(namev).upper()}

    if ltv == "level_ref":
        # v1 uses {type:"level_ref", name:"y_high"|..., minutes?:int}
        name = getattr(l, "name", None)
        refv = getattr(name, "value", None) if hasattr(name, "value") else (str(name).strip() if name is not None else None)
        if not refv:
            raise AlertValidationError(ERR_UNSUPPORTED_CONDITION, "ref level missing name")
        params: dict = {}
        mins = getattr(l, "minutes", None)
        if mins is not None and str(refv) in {"or_high", "or_low"}:
            try:
                params["minutes"] = int(mins)
            except Exception:
                pass
        return {"type": "ref", "ref": str(refv).lower(), "params": params}

    raise AlertValidationError(ERR_UNSUPPORTED_CONDITION, f"unsupported level in v1: type={ltv!r}")


def _expires_from_v1(exp) -> dict | None:
    if exp is None:
        return None

    et = getattr(exp, "type", None)
    etv = getattr(et, "value", None) if hasattr(et, "value") else (str(et).strip() if et is not None else None)

    # v1 default is EOD; strict schema does not have EOD, so use a conservative relative default.
    if etv == "eod":
        return {"type": "relative", "hours": 8}

    if etv == "duration":
        out: dict = {"type": "relative"}
        mins = getattr(exp, "minutes", None)
        hrs = getattr(exp, "hours", None)
        days = getattr(exp, "days", None)
        if mins is not None:
            out["minutes"] = int(mins)
        if hrs is not None:
            out["hours"] = int(hrs)
        if days is not None:
            out["days"] = int(days)
        if len(out) == 1:
            out["hours"] = 8
        return out

    if etv == "date":
        # strict schema expects YYYY-MM-DD and will coerce to date
        return {"type": "date", "date": str(getattr(exp, "date") or "").strip()}

    return None


def intent_v1_to_alert_intent(ai: AlertIntentV1):
    """Convert tnt_alerts.models.AlertIntentV1 -> tnt_alerts.alert_intent.AlertIntent.

    This is the inverse of alert_intent_to_intent_v1 and is used to keep
    deterministic (non-LLM) compilation compatible with the strict contract
    enforced by the evaluator + Redis store.
    """

    try:
        from tnt_alerts.alert_intent import AlertIntent as StrictAlertIntent
    except Exception as exc:  # pragma: no cover
        raise AlertValidationError(ERR_UNSUPPORTED_CONDITION, f"strict intent model unavailable: {type(exc).__name__}: {exc}")

    # Source
    src = getattr(ai, "source", None)
    created_at = getattr(src, "created_at_utc", None) if src is not None else None
    src_dict = {
        "user_id": str(getattr(src, "user_id", "") or ""),
        "channel_id": str(getattr(src, "channel_id", "") or ""),
        "request_text": str(getattr(src, "request_text", "") or ""),
        "created_at_utc": str(created_at).strip() if created_at else _now_utc_iso(),
    }

    # Targets
    tgt = getattr(ai, "targets", None)
    ttype = str(getattr(tgt, "type", "symbols") or "symbols")
    symbols = list(getattr(tgt, "symbols", []) or [])
    # Filter obvious timezone tokens that commonly appear near time windows.
    tz_stop = {"ET", "EDT", "EST", "CT", "CDT", "CST", "MT", "MDT", "MST", "PT", "PDT", "PST", "UTC", "GMT"}
    sym_out: list[str] = []
    for s in symbols:
        su = str(s or "").strip().upper()
        if not su:
            continue
        if su in tz_stop:
            continue
        sym_out.append(su)
    max_symbols = int(getattr(tgt, "max_symbols", 20) or 20)
    watchlist = getattr(tgt, "watchlist", None)
    targets_dict = {
        "type": "watchlist" if ttype == "watchlist" else "symbols",
        "symbols": sym_out,
        "watchlist": str(watchlist).strip() if watchlist else None,
        "max_symbols": max(1, min(max_symbols, 50)),
    }

    # Gates
    gates = getattr(ai, "gates", None)
    g_dict: dict = {}

    mh = getattr(gates, "market_hours", None) if gates is not None else None
    if mh is not None:
        sess = getattr(mh, "session", None)
        sess_s = str(sess).strip().upper() if sess else ""
        win = getattr(mh, "time_window_et", None)
        if win is not None:
            try:
                w0, w1 = win
                g_dict["market_hours"] = {"session": "CUSTOM", "time_window_et": [str(w0), str(w1)]}
            except Exception:
                g_dict["market_hours"] = {"session": "CUSTOM", "time_window_et": None}
        elif sess_s in {"RTH", "ETH"}:
            g_dict["market_hours"] = {"session": sess_s, "time_window_et": None}

    rg = getattr(gates, "regime", None) if gates is not None else None
    conf = getattr(gates, "confidence_min", None) if gates is not None else None
    if rg is not None or conf is not None:
        allowed = list(getattr(rg, "allowed", []) or []) if rg is not None else []
        allowed_s = [str(x).strip().upper() for x in allowed if str(x).strip()]
        if not allowed_s:
            allowed_s = ["BULLISH", "NEUTRAL", "BEARISH"]
        rg_dict: dict = {"allowed": allowed_s}
        if isinstance(conf, (int, float)):
            rg_dict["min_confidence"] = float(conf)
        g_dict["regime"] = rg_dict

    cd = getattr(gates, "cooldown", None) if gates is not None else None
    if cd is not None:
        try:
            g_dict["cooldown"] = {"seconds": int(getattr(cd, "seconds"))}
        except Exception:
            pass

    mt = getattr(gates, "max_triggers", None) if gates is not None else None
    if mt is not None:
        try:
            g_dict["max_triggers"] = {"count": int(mt)}
        except Exception:
            pass

    df = getattr(gates, "data_freshness", None) if gates is not None else None
    if df is not None:
        try:
            g_dict["data_freshness"] = {"price_age_seconds": int(getattr(df, "price_age_seconds"))}
        except Exception:
            pass

    # Lifecycle
    lc = getattr(ai, "lifecycle", None)
    exp = getattr(lc, "expires", None) if lc is not None else None
    lifecycle_dict = {"start": "now", "expires": _expires_from_v1(exp)}

    # Condition
    c = getattr(ai, "condition", None)
    ctype = str(getattr(c, "type", "") or "")
    tf = str(getattr(c, "timeframe", "5m") or "5m")

    confirm = getattr(c, "confirm", None)
    cmode = getattr(confirm, "mode", None) if confirm is not None else None
    cmode_s = str(getattr(cmode, "value", cmode) or "close").strip().lower() or "close"
    confirm_s = "intrabar" if cmode_s == "intrabar" else "close"

    if ctype == "cross":
        cond_dict = {
            "type": "cross",
            "op": str(getattr(c, "op", "crosses_above") or "crosses_above"),
            "left": _series_from_v1(getattr(c, "left")),
            "right": _series_from_v1(getattr(c, "right")),
            "timeframe": tf,
            "confirm": confirm_s,
        }
    elif ctype == "touch":
        cond_dict = {
            "type": "touch",
            "left": _series_from_v1(getattr(c, "left")),
            "right": _level_from_v1(getattr(c, "right")),
            "timeframe": tf,
            "confirm": confirm_s,
        }
    elif ctype == "break":
        cond_dict = {
            "type": "break",
            "op": str(getattr(c, "op", "breaks_above") or "breaks_above"),
            "level": _level_from_v1(getattr(c, "level")),
            "timeframe": tf,
            "confirm": confirm_s,
        }
    elif ctype == "new_high_low":
        op = str(getattr(c, "op", "new_high") or "new_high")
        kind = "new_high" if op == "new_high" else "new_low"
        lb = int(getattr(c, "lookback_days", 10) or 10)
        cond_dict = {
            "type": "new_extreme",
            "kind": kind,
            "lookback_bars": lb,
            "timeframe": "1D",
            "confirm": "close",
        }
    elif ctype == "rvol":
        cond_dict = {
            "type": "rvol",
            "threshold": float(getattr(c, "threshold", 2.0) or 2.0),
            "lookback_bars": int(getattr(c, "lookback_bars", 20) or 20),
            "timeframe": tf,
            "confirm": "close",
        }
    else:
        raise AlertValidationError(ERR_UNSUPPORTED_CONDITION, f"unsupported v1 condition type: {ctype!r}")

    # Actions
    actions_in = list(getattr(ai, "actions", []) or [])
    actions_out: list[dict] = []
    for a in actions_in:
        at = str(getattr(a, "type", "") or "")
        if at == "notify":
            style = str(getattr(a, "style", "compact") or "compact")
            actions_out.append({"type": "discord_notify", "style": style})
        elif at == "include_chart":
            chart = str(getattr(a, "chart", "execution") or "execution")
            actions_out.append({"type": "include_chart", "chart": chart})
        else:
            raise AlertValidationError(ERR_UNSUPPORTED_CONDITION, f"unsupported v1 action type: {at!r}")

    # Assemble
    payload = {
        "version": "1.0",
        "source": src_dict,
        "targets": targets_dict,
        "direction": str(getattr(getattr(ai, "direction", None), "value", getattr(ai, "direction", "AUTO")) or "AUTO"),
        "condition": cond_dict,
        "gates": g_dict,
        "lifecycle": lifecycle_dict,
        "actions": actions_out or [{"type": "discord_notify", "style": "compact"}],
        "tags": dict(getattr(ai, "tags", {}) or {}),
    }

    return StrictAlertIntent.model_validate(payload)


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
