from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

# --- Enums / canonical strings ------------------------------------------------

Timeframe = Literal["1m", "2m", "3m", "5m", "10m", "15m", "30m", "60m", "1D"]
SessionName = Literal["RTH", "ETH"]

ConfirmMode = Literal["close", "intrabar"]


# --- Source / targeting -------------------------------------------------------


class AlertSource(BaseModel):
    user_id: str
    channel_id: str
    request_text: str


class AlertTargets(BaseModel):
    type: Literal["symbols", "watchlist"] = "symbols"
    symbols: list[str] = Field(default_factory=list)
    watchlist: str | None = None
    max_symbols: int = 20


# --- Expressions --------------------------------------------------------------


class SeriesPrice(BaseModel):
    type: Literal["series"] = "series"
    name: Literal["price"] = "price"


class IndicatorVWAP(BaseModel):
    type: Literal["indicator"] = "indicator"
    name: Literal["vwap"] = "vwap"
    params: dict[str, Any] = Field(default_factory=dict)


class IndicatorSMA(BaseModel):
    type: Literal["indicator"] = "indicator"
    name: Literal["sma"] = "sma"
    params: dict[str, Any] = Field(default_factory=dict)


class IndicatorEMA(BaseModel):
    type: Literal["indicator"] = "indicator"
    name: Literal["ema"] = "ema"
    params: dict[str, Any] = Field(default_factory=dict)


Indicator = IndicatorVWAP | IndicatorSMA | IndicatorEMA
Series = SeriesPrice | Indicator


class PivotRef(BaseModel):
    type: Literal["pivot"] = "pivot"
    name: Literal["P", "R1", "R2", "S1", "S2"]


class LevelRef(BaseModel):
    type: Literal["level_ref"] = "level_ref"
    name: Literal["y_high", "y_low", "or_high", "or_low"]
    minutes: int | None = None


class LevelNumber(BaseModel):
    type: Literal["number"] = "number"
    value: float


Level = PivotRef | LevelRef | LevelNumber


# --- Confirm -----------------------------------------------------------------


class ConfirmSpec(BaseModel):
    mode: Literal["close", "intrabar", "n_closes"] = "close"
    n: int | None = None


# --- Conditions ---------------------------------------------------------------


class ConditionCross(BaseModel):
    type: Literal["cross"] = "cross"
    op: Literal["crosses_above", "crosses_below"]
    left: Series
    right: Series
    timeframe: Timeframe
    confirm: ConfirmSpec = Field(default_factory=ConfirmSpec)


class ConditionTouch(BaseModel):
    type: Literal["touch"] = "touch"
    left: Series
    right: Level
    timeframe: Timeframe
    confirm: ConfirmSpec = Field(default_factory=lambda: ConfirmSpec(mode="intrabar"))


class ConditionBreak(BaseModel):
    type: Literal["break"] = "break"
    op: Literal["breaks_above", "breaks_below"]
    level: LevelRef
    timeframe: Timeframe
    confirm: ConfirmSpec = Field(default_factory=ConfirmSpec)


class ConditionNewHighLow(BaseModel):
    type: Literal["new_high_low"] = "new_high_low"
    op: Literal["new_high", "new_low"]
    lookback_days: int
    timeframe: Literal["1D"] = "1D"


class ConditionRvol(BaseModel):
    type: Literal["rvol"] = "rvol"
    lookback_bars: int = 20
    threshold: float
    timeframe: Timeframe


Condition = ConditionCross | ConditionTouch | ConditionBreak | ConditionNewHighLow | ConditionRvol


# --- Gates / session / expiry -------------------------------------------------


class RegimeGate(BaseModel):
    allowed: list[Literal["BULLISH", "NEUTRAL", "BEARISH", "TRANSITION"]] = Field(default_factory=list)


class MarketHoursGate(BaseModel):
    session: SessionName | None = "RTH"
    time_window_et: tuple[str, str] | None = None


class CooldownGate(BaseModel):
    seconds: int = 300


class DataFreshnessGate(BaseModel):
    price_age_seconds: int = 60


class Gates(BaseModel):
    regime: RegimeGate | None = None
    confidence_min: float | None = None
    market_hours: MarketHoursGate | None = None
    cooldown: CooldownGate | None = None
    max_triggers: int = 3
    data_freshness: DataFreshnessGate | None = None


class ExpiresEOD(BaseModel):
    type: Literal["eod"] = "eod"


class ExpiresDuration(BaseModel):
    type: Literal["duration"] = "duration"
    minutes: int | None = None
    hours: int | None = None
    days: int | None = None


class ExpiresDate(BaseModel):
    type: Literal["date"] = "date"
    date: str


Expires = ExpiresEOD | ExpiresDuration | ExpiresDate


class Lifecycle(BaseModel):
    start: Literal["now"] = "now"
    expires: Expires


# --- Actions -----------------------------------------------------------------


class ActionNotify(BaseModel):
    type: Literal["notify"] = "notify"
    style: Literal["compact", "verbose"] = "compact"


class ActionIncludeChart(BaseModel):
    type: Literal["include_chart"] = "include_chart"
    chart: Literal["execution", "context"] = "execution"


Action = ActionNotify | ActionIncludeChart


class RiskSpec(BaseModel):
    severity: Literal["low", "normal", "high"] = "normal"
    notes: str | None = None


class AlertIntentV1(BaseModel):
    version: Literal["1.0"] = "1.0"
    source: AlertSource
    targets: AlertTargets
    condition: Condition
    gates: Gates = Field(default_factory=Gates)
    lifecycle: Lifecycle
    actions: list[Action] = Field(default_factory=lambda: [ActionNotify()])
    risk: RiskSpec = Field(default_factory=RiskSpec)


class StoredAlertState(BaseModel):
    status: Literal["active", "paused", "deleted"] = "active"
    created_at_utc: str
    updated_at_utc: str


class StoredAlert(BaseModel):
    id: str
    intent: AlertIntentV1
    state: StoredAlertState
