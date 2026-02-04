from __future__ import annotations

import re
from dataclasses import dataclass

from .errors import AlertCompileError
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
from .reasons import (
    ERR_INVALID_NUMBER,
    ERR_MISSING_WHEN,
    ERR_UNSUPPORTED_CONDITION,
    ERR_UNSUPPORTED_TIMEFRAME,
    PARSE_AMBIGUOUS,
    PARSE_OK,
    WARN_DEFAULT_CONFIRM_USED,
    WARN_DEFAULT_SESSION_USED,
    WARN_DEFAULT_TIMEFRAME_USED,
)
from .validator import validate_intent

_ALLOWED_TF = ["1m", "2m", "3m", "5m", "10m", "15m", "30m", "60m", "1D"]


@dataclass(frozen=True)
class CompileOutput:
    parse_code: str
    warnings: list[str]
    intents: list[AlertIntentV1]
    resolution_note: str | None = None


_TICKER_RE = re.compile(r"\b[A-Z][A-Z0-9]{0,9}(?:[\.-][A-Z0-9]{1,10})?\b")


def _nearest_tf(minutes: int) -> str:
    choices = [1, 2, 3, 5, 10, 15, 30, 60]
    best = min(choices, key=lambda x: abs(x - minutes))
    return f"{best}m" if best != 60 else "60m"


def _parse_timeframe(text: str) -> tuple[str | None, str | None]:
    t = (text or "").lower()
    m = re.search(r"\bON\s+(1m|2m|3m|5m|10m|15m|30m|60m|1D)\b", text, re.I)
    if m:
        return m.group(1), None

    m2 = re.search(r"\b(\d+)\s*(m|min|minute|minutes)\b", t)
    if m2:
        mins = int(m2.group(1))
        tf = f"{mins}m" if mins != 60 else "60m"
        if tf not in _ALLOWED_TF:
            sug = _nearest_tf(mins)
            raise AlertCompileError(ERR_UNSUPPORTED_TIMEFRAME, f"unsupported timeframe: {tf}", suggestion=f"Use {sug}.")
        return tf, None

    if re.search(r"\b(\d+)\s*[- ]?day\s+sma\b", t) or re.search(r"\b(\d+)\s*d\s*sma\b", t):
        return "1D", None

    return None, None


def _parse_expiry(text: str) -> tuple[ExpiresEOD | ExpiresDuration | ExpiresDate, str | None]:
    t = (text or "").strip().lower()

    m_date = re.search(r"\buntil\s+(\d{4}-\d{2}-\d{2})\b", t)
    if m_date:
        return ExpiresDate(date=m_date.group(1)), None

    if "until eod" in t or "until end of day" in t or re.search(r"\buntil\s+eod\b", t):
        return ExpiresEOD(), None

    m = re.search(r"\b(next|for|until)\s+(\d+)\s*(d|day|days)\b", t)
    if m:
        return ExpiresDuration(days=int(m.group(2))), None

    m2 = re.search(r"\b(next|for|until)\s+(\d+)\s*(h|hour|hours)\b", t)
    if m2:
        return ExpiresDuration(hours=int(m2.group(2))), None

    return ExpiresEOD(), None


def _parse_targets(text: str) -> AlertTargets:
    raw = (text or "").strip()

    m = re.search(r"watchlist\s*:\s*(.+)", raw, re.I)
    if m:
        rest = m.group(1).strip()
        m_when = re.search(r"\bWHEN\b", rest, re.I)
        name = rest[: m_when.start()].strip() if m_when else rest.strip()
        return AlertTargets(type="watchlist", watchlist=name, symbols=[], max_symbols=20)

    m_when = re.search(r"\bWHEN\b", raw, re.I)
    prefix = raw
    suffix = ""
    if m_when:
        prefix = raw[: m_when.start()]
        suffix = raw[m_when.end() :]

    stop = {
        "WHEN",
        "ONLY",
        "DURING",
        "UNTIL",
        "NOTIFY",
        "ME",
        "ABOVE",
        "BELOW",
        "OVER",
        "UNDER",
        "VWAP",
        "ON",
        "RTH",
        "ETH",
        "EOD",
        "IF",
        "REGIME",
        "BULLISH",
        "BEARISH",
        "NEUTRAL",
        "TRANSITION",
        "CONFIDENCE",
        "CHART",
        "RVOL",
        "BREAKS",
        "CROSSES",
        "TOUCHES",
        "HIGH",
        "LOW",
        "YESTERDAY",
        "LAST",
        "RANGE",
        "MIN",
        "MINS",
        "MINUTE",
        "MINUTES",
        "HOUR",
        "HOURS",
        "DAY",
        "DAYS",
        "NEXT",
        "FOR",
        "UNTIL",
        "TODAY",
        "BETWEEN",
        "AND",

        # Common keywords that are ALL-CAPS in requests but are not tickers
        "PING",
        "ALERT",
        "VWAP",
        "SMA",
        "EMA",
        "RVOL",
        "RSI",
        "MACD",

        # Time zones / common time markers that are not tickers.
        "ET",
        "EDT",
        "EST",
        "CT",
        "CDT",
        "CST",
        "MT",
        "MDT",
        "MST",
        "PT",
        "PDT",
        "PST",
        "UTC",
        "GMT",
        "AM",
        "PM",
    }

    def _extract(seg: str) -> list[str]:
        found: list[str] = []
        for s in re.findall(r"\$([A-Za-z][A-Za-z0-9]{0,9})", seg or ""):
            s_up = s.upper()
            if s_up not in stop:
                found.append(s_up)
        for s in re.findall(r"\^([A-Za-z][A-Za-z0-9]{0,9})", seg or ""):
            s_up = s.upper()
            if s_up not in stop:
                found.append(s_up)
        for s in _TICKER_RE.findall(seg):
            s_up = s.upper()
            if s_up in stop:
                continue
            if 1 <= len(s_up) <= 10:
                found.append(s_up)
        out: list[str] = []
        seen: set[str] = set()
        for s in found:
            if s not in seen:
                out.append(s)
                seen.add(s)
        return out

    syms = _extract(prefix)
    if not syms and suffix:
        syms = _extract(suffix[:120])

    if not syms and re.search(r"\bportfolio\b", raw, re.I):
        return AlertTargets(type="watchlist", watchlist="PORTFOLIO", symbols=[], max_symbols=20)

    return AlertTargets(type="symbols", symbols=syms, max_symbols=20)


def _parse_market_hours(text: str) -> tuple[MarketHoursGate | None, str | None]:
    t = (text or "").lower()
    m = re.search(r"\b(\d{2}:\d{2})\s*(?:-|to|–|—|and)\s*(\d{2}:\d{2})\b", t)
    if m:
        return MarketHoursGate(session=None, time_window_et=(m.group(1), m.group(2))), None

    if "rth" in t or "regular" in t:
        return MarketHoursGate(session="RTH", time_window_et=None), None

    if "eth" in t:
        return MarketHoursGate(session="ETH", time_window_et=None), None

    # Default session is RTH; do not treat as a warning for DSL previews.
    return MarketHoursGate(session="RTH", time_window_et=None), None


def _parse_regime_gate(text: str) -> RegimeGate | None:
    t = (text or "").lower()
    if "regime" not in t and "chop" not in t and "transition" not in t:
        return None

    if "not" in t and "transition" in t:
        return RegimeGate(allowed=["BULLISH", "NEUTRAL", "BEARISH"])

    allowed: list[str] = []
    if "bullish" in t:
        allowed.append("BULLISH")
    if "neutral" in t:
        allowed.append("NEUTRAL")
    if "bearish" in t:
        allowed.append("BEARISH")
    if "transition" in t or "chop" in t:
        allowed.append("TRANSITION")
    return RegimeGate(allowed=allowed) if allowed else None


def _parse_confidence_gate(text: str) -> float | None:
    t = (text or "").lower()
    m = re.search(r"confidence\s*(>=|>|=)\s*(0?\.\d+|1\.0+|1)", t)
    if m:
        try:
            return float(m.group(2))
        except Exception:
            return None

    m2 = re.search(r"confidence\s*(?:is\s*)?(above|over)\s*(0?\.\d+|1\.0+|1)", t)
    if m2:
        try:
            return float(m2.group(2))
        except Exception:
            return None

    return None


def _parse_cooldown_gate(text: str) -> CooldownGate | None:
    t = (text or "").lower()
    if "don\u2019t spam" in t or "dont spam" in t or "don't spam" in t:
        return CooldownGate(seconds=300)

    m = re.search(r"cooldown\s+(\d+)\s*(s|sec|secs|second|seconds|m|min|minute|minutes)", t)
    if not m:
        return None
    n = int(m.group(1))
    unit = m.group(2)
    seconds = n if unit.startswith("s") else n * 60
    return CooldownGate(seconds=seconds)


def _parse_max_triggers(text: str) -> int | None:
    t = (text or "").lower()
    m = re.search(r"max[_\s-]?triggers\s+(\d+)", t)
    if m:
        return int(m.group(1))
    return None


def _parse_price_age_gate(text: str) -> DataFreshnessGate | None:
    t = (text or "").lower()
    if "data is fresh" in t or "fresh data" in t:
        return DataFreshnessGate(price_age_seconds=60)
    m = re.search(r"price_age\s*<=\s*(\d+)\s*(s|sec|secs|second|seconds)", t)
    if m:
        return DataFreshnessGate(price_age_seconds=int(m.group(1)))
    return None


def _parse_indicator_sma(text: str) -> IndicatorSMA | None:
    t = (text or "").lower()
    m = re.search(r"\b(\d+)\s*[- ]?day\s+sma\b", t)
    if m:
        return IndicatorSMA(params={"n": int(m.group(1))})
    m_rev = re.search(r"\b(\d+)\s*sma\b", t)
    if m_rev:
        return IndicatorSMA(params={"n": int(m_rev.group(1))})
    m2 = re.search(r"\bsma\s*\(?\s*(\d+)\s*\)?\b", t)
    if m2:
        return IndicatorSMA(params={"n": int(m2.group(1))})
    return None


def _parse_indicator_ema(text: str) -> IndicatorEMA | None:
    t = (text or "").lower()
    m = re.search(r"\bema\s*\(?\s*(\d+)\s*\)?\b", t)
    if m:
        return IndicatorEMA(params={"n": int(m.group(1))})
    return None


def _parse_touch_pivot(text: str) -> PivotRef | None:
    t = (text or "").upper()
    m = re.search(r"\b(pivot\s*\(\s*)?(P|R1|R2|S1|S2)\s*\)?\b", t)
    if not m:
        return None
    return PivotRef(name=m.group(2))


def _parse_or_minutes(text: str) -> int | None:
    t = (text or "").lower()
    m = re.search(r"\b(\d+)\s*(minute|minutes|min|m)\s+opening\s+range\b", t)
    if m:
        return int(m.group(1))
    return None


def _parse_condition_dsl(text: str, *, tf: str) -> tuple[list[object], str, list[str]]:
    warnings: list[str] = []
    parse_code = PARSE_OK
    raw = text.strip()

    m = re.search(r"\b(crosses_above|crosses_below)\s*\(\s*(price|vwap|sma\(\d+\)|ema\(\d+\))\s*,\s*(price|vwap|sma\(\d+\)|ema\(\d+\))\s*\)", raw, re.I)
    if m:
        op = m.group(1).lower()
        left_s = m.group(2).lower()
        right_s = m.group(3).lower()

        def _series(s: str):
            if s == "price":
                return SeriesPrice()
            if s == "vwap":
                return IndicatorVWAP()
            if s.startswith("sma("):
                n = int(re.search(r"\d+", s).group(0))
                return IndicatorSMA(params={"n": n})
            if s.startswith("ema("):
                n = int(re.search(r"\d+", s).group(0))
                return IndicatorEMA(params={"n": n})
            return SeriesPrice()

        confirm = ConfirmSpec(mode="close")
        cm = re.search(r"\bCONFIRM\s+(close|intrabar|n_closes\(\d+\))\b", raw, re.I)
        if cm:
            c = cm.group(1).lower()
            if c.startswith("n_closes"):
                n = int(re.search(r"\d+", c).group(0))
                confirm = ConfirmSpec(mode="n_closes", n=n)
            else:
                confirm = ConfirmSpec(mode=c)  # type: ignore[arg-type]
        else:
            warnings.append(WARN_DEFAULT_CONFIRM_USED)

        cond = ConditionCross(
            op=op,  # type: ignore[arg-type]
            left=_series(left_s),
            right=_series(right_s),
            timeframe=tf,  # type: ignore[arg-type]
            confirm=confirm,
        )
        return [cond], parse_code, warnings

    m2 = re.search(r"\btouches\s*\(\s*(price|vwap)\s*,\s*(pivot\((P|R1|R2|S1|S2)\)|y_high|y_low|or_high\(\d+\)|or_low\(\d+\)|\d+(?:\.\d+)?)\s*\)", raw, re.I)
    if m2:
        left = SeriesPrice() if m2.group(1).lower() == "price" else IndicatorVWAP()
        rhs = m2.group(2)
        if rhs.lower().startswith("pivot"):
            lev = PivotRef(name=m2.group(3))
        elif rhs.lower() in {"y_high", "y_low"}:
            lev = LevelRef(name=rhs.lower(), minutes=None)
        elif rhs.lower().startswith("or_high"):
            mins = int(re.search(r"\d+", rhs).group(0))
            lev = LevelRef(name="or_high", minutes=mins)
        elif rhs.lower().startswith("or_low"):
            mins = int(re.search(r"\d+", rhs).group(0))
            lev = LevelRef(name="or_low", minutes=mins)
        else:
            lev = LevelNumber(value=float(rhs))

        confirm = ConfirmSpec(mode="intrabar")
        cm = re.search(r"\bCONFIRM\s+(close|intrabar|n_closes\(\d+\))\b", raw, re.I)
        if cm:
            c = cm.group(1).lower()
            if c.startswith("n_closes"):
                n = int(re.search(r"\d+", c).group(0))
                confirm = ConfirmSpec(mode="n_closes", n=n)
            else:
                confirm = ConfirmSpec(mode=c)  # type: ignore[arg-type]
        else:
            warnings.append(WARN_DEFAULT_CONFIRM_USED)

        return [ConditionTouch(left=left, right=lev, timeframe=tf, confirm=confirm)], parse_code, warnings  # type: ignore[arg-type]

    m3 = re.search(r"\b(breaks_above|breaks_below)\s*\(\s*(y_high|y_low|or_high\(\d+\)|or_low\(\d+\))\s*\)", raw, re.I)
    if m3:
        op = m3.group(1).lower()
        rhs = m3.group(2).lower()
        if rhs in {"y_high", "y_low"}:
            lev = LevelRef(name=rhs, minutes=None)
        else:
            mins = int(re.search(r"\d+", rhs).group(0))
            lev = LevelRef(name="or_high" if rhs.startswith("or_high") else "or_low", minutes=mins)

        confirm = ConfirmSpec(mode="close")
        cm = re.search(r"\bCONFIRM\s+(close|intrabar|n_closes\(\d+\))\b", raw, re.I)
        if cm:
            c = cm.group(1).lower()
            if c.startswith("n_closes"):
                n = int(re.search(r"\d+", c).group(0))
                confirm = ConfirmSpec(mode="n_closes", n=n)
            else:
                confirm = ConfirmSpec(mode=c)  # type: ignore[arg-type]
        else:
            warnings.append(WARN_DEFAULT_CONFIRM_USED)

        return [ConditionBreak(op=op, level=lev, timeframe=tf, confirm=confirm)], parse_code, warnings  # type: ignore[arg-type]

    m4 = re.search(r"\b(new_high|new_low)\s*\(\s*(\d+)d\s*\)", raw, re.I)
    if m4:
        op = m4.group(1).lower()
        days = int(m4.group(2))
        return [ConditionNewHighLow(op=op, lookback_days=days)], parse_code, warnings  # type: ignore[arg-type]

    m5 = re.search(r"\brvol\s*\(\s*(\d+)\s*\)\s*>\s*(\d+(?:\.\d+)?)", raw, re.I)
    if m5:
        lb = int(m5.group(1))
        thr = float(m5.group(2))
        return [ConditionRvol(lookback_bars=lb, threshold=thr, timeframe=tf)], parse_code, warnings  # type: ignore[arg-type]

    raise AlertCompileError(ERR_UNSUPPORTED_CONDITION, "unsupported DSL condition")


def _parse_condition_english(text: str, *, tf: str) -> tuple[list[object], str, list[str]]:
    t = (text or "").lower()
    warnings: list[str] = []
    parse_code = PARSE_OK

    if "vwap" in t:
        # Explicitly ambiguous direction (user asked for both ways): require clarification.
        if "either way" in t or "both" in t:
            parse_code = PARSE_AMBIGUOUS
            return (
                [
                    ConditionCross(op="crosses_above", left=SeriesPrice(), right=IndicatorVWAP(), timeframe=tf, confirm=ConfirmSpec(mode="close")),
                    ConditionCross(op="crosses_below", left=SeriesPrice(), right=IndicatorVWAP(), timeframe=tf, confirm=ConfirmSpec(mode="close")),
                ],
                parse_code,
                [],
            )

        # Missing direction: default to crosses_below (downside break) without forcing clarify.
        if "cross" in t and not any(k in t for k in ("above", "below", "over", "under")):
            return [ConditionCross(op="crosses_below", left=SeriesPrice(), right=IndicatorVWAP(), timeframe=tf, confirm=ConfirmSpec(mode="close"))], PARSE_OK, []

        if "above" in t or "over" in t:
            return [ConditionCross(op="crosses_above", left=SeriesPrice(), right=IndicatorVWAP(), timeframe=tf, confirm=ConfirmSpec(mode="close"))], parse_code, warnings
        if "below" in t or "under" in t:
            return [ConditionCross(op="crosses_below", left=SeriesPrice(), right=IndicatorVWAP(), timeframe=tf, confirm=ConfirmSpec(mode="close"))], parse_code, warnings

        if "break" in t or "breaks" in t:
            # Users often say "breaks VWAP" meaning a downside break; default to crosses_below.
            return [ConditionCross(op="crosses_below", left=SeriesPrice(), right=IndicatorVWAP(), timeframe=tf, confirm=ConfirmSpec(mode="close"))], PARSE_OK, warnings

        raise AlertCompileError(
            ERR_UNSUPPORTED_CONDITION,
            "VWAP condition needs direction",
            suggestion="Say 'crosses above VWAP' or 'crosses below VWAP'.",
        )

    if "yesterday" in t and "high" in t:
        return [ConditionBreak(op="breaks_above", level=LevelRef(name="y_high"), timeframe=tf, confirm=ConfirmSpec(mode="close"))], parse_code, warnings
    if "yesterday" in t and "low" in t:
        return [ConditionBreak(op="breaks_below", level=LevelRef(name="y_low"), timeframe=tf, confirm=ConfirmSpec(mode="close"))], parse_code, warnings

    if "opening range" in t and "high" in t:
        mins = _parse_or_minutes(text) or 15
        return [ConditionBreak(op="breaks_above", level=LevelRef(name="or_high", minutes=mins), timeframe=tf, confirm=ConfirmSpec(mode="close"))], parse_code, warnings
    if "opening range" in t and "low" in t:
        mins = _parse_or_minutes(text) or 15
        return [ConditionBreak(op="breaks_below", level=LevelRef(name="or_low", minutes=mins), timeframe=tf, confirm=ConfirmSpec(mode="close"))], parse_code, warnings

    if "touch" in t:
        piv = _parse_touch_pivot(text)
        if piv:
            return [ConditionTouch(left=SeriesPrice(), right=piv, timeframe=tf, confirm=ConfirmSpec(mode="intrabar"))], parse_code, warnings

    sma = _parse_indicator_sma(text)
    if sma:
        if "below" in t or "under" in t:
            return [ConditionCross(op="crosses_below", left=SeriesPrice(), right=sma, timeframe="1D", confirm=ConfirmSpec(mode="close"))], parse_code, warnings
        if "above" in t or "over" in t:
            return [ConditionCross(op="crosses_above", left=SeriesPrice(), right=sma, timeframe="1D", confirm=ConfirmSpec(mode="close"))], parse_code, warnings
        raise AlertCompileError(ERR_UNSUPPORTED_CONDITION, "SMA cross needs direction", suggestion="Say 'crosses above' or 'crosses below'.")

    ema = _parse_indicator_ema(text)
    if ema:
        if "below" in t or "under" in t:
            return [ConditionCross(op="crosses_below", left=SeriesPrice(), right=ema, timeframe=tf, confirm=ConfirmSpec(mode="close"))], parse_code, warnings
        if "above" in t or "over" in t:
            return [ConditionCross(op="crosses_above", left=SeriesPrice(), right=ema, timeframe=tf, confirm=ConfirmSpec(mode="close"))], parse_code, warnings
        raise AlertCompileError(ERR_UNSUPPORTED_CONDITION, "EMA cross needs direction")

    mhl = re.search(r"new\s+(\d+)[- ]?day\s+(high|low)", t)
    if mhl:
        days = int(mhl.group(1))
        op = "new_high" if mhl.group(2) == "high" else "new_low"
        return [ConditionNewHighLow(op=op, lookback_days=days)], parse_code, warnings

    mrv = re.search(r"rvol\s*(>=|>|=)\s*(\d+(?:\.\d+)?)", t)
    if not mrv:
        mrv = re.search(r"rvol\s*(?:is\s*)?(over|above)\s*(\d+(?:\.\d+)?)", t)
    if mrv:
        thr = float(mrv.group(2))
        return [ConditionRvol(lookback_bars=20, threshold=thr, timeframe=tf)], parse_code, warnings

    raise AlertCompileError(
        ERR_UNSUPPORTED_CONDITION,
        "Unsupported or unclear condition",
        suggestion="Examples: 'crosses above VWAP', 'breaks yesterday high', 'rvol > 2'.",
    )


def compile_request(*, request_text: str, user_id: str, channel_id: str) -> CompileOutput:
    raw = (request_text or "").strip()
    if not raw:
        raise AlertCompileError(ERR_INVALID_NUMBER, "empty request")

    if re.search(r"\bWHEN\b", raw, re.I) is None and raw.strip().upper().startswith("WATCHLIST:"):
        raise AlertCompileError(ERR_MISSING_WHEN, "missing WHEN clause", suggestion="Example: watchlist:default WHEN rvol(20) > 2 ON 15m")

    targets = _parse_targets(raw)
    if targets.type == "symbols" and not (targets.symbols or []):
        raise AlertCompileError(
            ERR_INVALID_NUMBER,
            "no symbols found in request",
            suggestion="Include symbols like 'SPY, QQQ' or use 'watchlist:<name> WHEN ...'.",
        )
    tf, warn_tf = _parse_timeframe(raw)
    warnings: list[str] = []
    if warn_tf:
        warnings.append(warn_tf)

    if tf is None:
        if re.search(r"\b(\d+)\s*[- ]?day\s+sma\b", raw, re.I) or re.search(r"\b(\d+)\s*d\s*sma\b", raw, re.I):
            tf = "1D"
            # Inferred from the request; not considered a warning for UX.
        elif "opening range" in raw.lower():
            tf = "1m"
            # Inferred from the request; not considered a warning for UX.
        else:
            tf = "5m"
            # Default to 5m for intraday requests; warnings (if any) are added at the envelope layer.

    expires, _ = _parse_expiry(raw)
    if tf == "1D" and isinstance(expires, ExpiresEOD):
        expires = ExpiresDuration(days=30)

    mh, _ = _parse_market_hours(raw)

    # Anti-spam gates are opt-in.
    cooldown = _parse_cooldown_gate(raw)
    max_triggers = _parse_max_triggers(raw)
    if cooldown is not None and max_triggers is None:
        # A simple "don't spam" request implies both defaults.
        max_triggers = 3

    gates = Gates(
        regime=_parse_regime_gate(raw),
        confidence_min=_parse_confidence_gate(raw),
        market_hours=mh,
        cooldown=cooldown,
        max_triggers=max_triggers,
        data_freshness=_parse_price_age_gate(raw),
    )

    actions = [ActionNotify(style="compact")]
    if "include chart" in raw.lower() or "include_chart" in raw.lower() or "chart" in raw.lower():
        actions.append(ActionIncludeChart(chart="execution"))

    parse_code = PARSE_OK
    resolution_note: str | None = None

    if re.search(r"\bWHEN\b", raw, re.I) and re.search(r"\b(crosses_above|crosses_below|touches\(|breaks_above|breaks_below|new_high\(|new_low\(|rvol\()", raw, re.I):
        conds, parse_code, cond_warnings = _parse_condition_dsl(raw, tf=tf)
    else:
        conds, parse_code, cond_warnings = _parse_condition_english(raw, tf=tf)

    warnings.extend(cond_warnings)
    if parse_code == PARSE_AMBIGUOUS:
        resolution_note = "ambiguous input resolved"

    intents: list[AlertIntentV1] = []
    for cond in conds:
        intent = AlertIntentV1(
            source=AlertSource(user_id=user_id, channel_id=channel_id, request_text=request_text),
            targets=targets,
            condition=cond,  # type: ignore[arg-type]
            gates=gates,
            lifecycle=Lifecycle(start="now", expires=expires),
            actions=actions,
        )

        intent2, val_warn = validate_intent(intent)
        warnings.extend(val_warn)
        intents.append(intent2)

    return CompileOutput(parse_code=parse_code, warnings=warnings, intents=intents, resolution_note=resolution_note)
