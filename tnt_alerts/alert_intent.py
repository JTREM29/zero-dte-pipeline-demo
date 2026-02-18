from __future__ import annotations

from datetime import date, datetime
from enum import Enum
from typing import Any, Dict, List, Literal, Optional, Union

from pydantic import BaseModel, Field, field_validator, model_validator


# -------------------------
# Enums
# -------------------------


class Timeframe(str, Enum):
    m1 = "1m"
    m2 = "2m"
    m3 = "3m"
    m5 = "5m"
    m10 = "10m"
    m15 = "15m"
    m30 = "30m"
    m60 = "60m"
    d1 = "1D"


class Session(str, Enum):
    RTH = "RTH"
    ETH = "ETH"
    CUSTOM = "CUSTOM"


class Regime(str, Enum):
    BULLISH = "BULLISH"
    NEUTRAL = "NEUTRAL"
    BEARISH = "BEARISH"
    TRANSITION = "TRANSITION"


class ConfirmMode(str, Enum):
    close = "close"
    intrabar = "intrabar"


class ActionType(str, Enum):
    discord_notify = "discord_notify"
    include_chart = "include_chart"


class NotifyStyle(str, Enum):
    compact = "compact"
    verbose = "verbose"


class ChartKind(str, Enum):
    execution = "execution"
    context = "context"


# -------------------------
# Source metadata
# -------------------------


class SourceMeta(BaseModel):
    user_id: str
    channel_id: str
    request_text: str
    created_at_utc: datetime = Field(default_factory=lambda: datetime.utcnow())


# -------------------------
# Targets
# -------------------------


class TargetsType(str, Enum):
    symbols = "symbols"
    watchlist = "watchlist"


class Targets(BaseModel):
    type: TargetsType
    symbols: List[str] = Field(default_factory=list)
    watchlist: Optional[str] = None
    max_symbols: int = 20

    @model_validator(mode="after")
    def _validate_targets(self) -> "Targets":
        if self.type == TargetsType.symbols:
            if not self.symbols:
                raise ValueError("targets.symbols required when targets.type='symbols'")
            if self.watchlist is not None:
                raise ValueError("targets.watchlist must be null when targets.type='symbols'")
        else:
            if not self.watchlist:
                raise ValueError("targets.watchlist required when targets.type='watchlist'")
            if self.symbols:
                raise ValueError("targets.symbols must be empty when targets.type='watchlist'")
        return self

    @field_validator("symbols")
    @classmethod
    def _normalize_symbols(cls, v: List[str]) -> List[str]:
        out: List[str] = []
        for s in v:
            s2 = (s or "").strip().upper()
            if not s2:
                continue
            out.append(s2)
        return out


# -------------------------
# Series & Levels
# -------------------------


class SeriesType(str, Enum):
    price = "price"
    indicator = "indicator"


class IndicatorName(str, Enum):
    vwap = "vwap"
    sma = "sma"
    ema = "ema"


class Series(BaseModel):
    type: SeriesType
    field: Optional[str] = None
    name: Optional[IndicatorName] = None
    params: Dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _validate_series(self) -> "Series":
        if self.type == SeriesType.price:
            if self.name is not None:
                raise ValueError("Series.name must be null for type='price'")
            if self.field is None:
                self.field = "last"  # type: ignore[misc]
        else:
            if self.name is None:
                raise ValueError("Series.name required for type='indicator'")
        return self


class LevelType(str, Enum):
    number = "number"
    pivot = "pivot"
    ref = "ref"


class PivotName(str, Enum):
    P = "P"
    R1 = "R1"
    R2 = "R2"
    S1 = "S1"
    S2 = "S2"


class LevelRefName(str, Enum):
    y_high = "y_high"
    y_low = "y_low"
    or_high = "or_high"
    or_low = "or_low"


class Level(BaseModel):
    type: LevelType
    value: Optional[float] = None
    pivot: Optional[PivotName] = None
    ref: Optional[LevelRefName] = None
    params: Dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _validate_level(self) -> "Level":
        if self.type == LevelType.number:
            if self.value is None:
                raise ValueError("Level.value required when type='number'")
        elif self.type == LevelType.pivot:
            if self.pivot is None:
                raise ValueError("Level.pivot required when type='pivot'")
        else:
            if self.ref is None:
                raise ValueError("Level.ref required when type='ref'")
            if self.ref in (LevelRefName.or_high, LevelRefName.or_low):
                mins = self.params.get("minutes")
                if not isinstance(mins, int) or mins <= 0 or mins > 180:
                    raise ValueError("or_high/or_low require params.minutes as int 1..180")
        return self


# -------------------------
# Conditions
# -------------------------


class ConditionBase(BaseModel):
    timeframe: Timeframe
    confirm: ConfirmMode = ConfirmMode.close


class CrossOp(str, Enum):
    crosses_above = "crosses_above"
    crosses_below = "crosses_below"


class CrossCondition(ConditionBase):
    type: Literal["cross"] = "cross"
    op: CrossOp
    left: Series
    right: Series


class TouchCondition(ConditionBase):
    type: Literal["touch"] = "touch"
    left: Series
    right: Level


class BreakOp(str, Enum):
    breaks_above = "breaks_above"
    breaks_below = "breaks_below"


class BreakCondition(ConditionBase):
    type: Literal["break"] = "break"
    op: BreakOp
    level: Level


class NewExtremeKind(str, Enum):
    new_high = "new_high"
    new_low = "new_low"


class NewExtremeCondition(ConditionBase):
    type: Literal["new_extreme"] = "new_extreme"
    kind: NewExtremeKind
    lookback_bars: int = 10

    @field_validator("lookback_bars")
    @classmethod
    def _lb_positive(cls, v: int) -> int:
        if v <= 1 or v > 5000:
            raise ValueError("lookback_bars must be 2..5000")
        return v


class RVOLCondition(ConditionBase):
    type: Literal["rvol"] = "rvol"
    threshold: float = 2.0
    lookback_bars: int = 20

    @field_validator("threshold")
    @classmethod
    def _thr_ok(cls, v: float) -> float:
        if v <= 0 or v > 50:
            raise ValueError("threshold must be (0, 50]")
        return v


Condition = Union[CrossCondition, TouchCondition, BreakCondition, NewExtremeCondition, RVOLCondition]


# -------------------------
# Gates
# -------------------------


class RegimeGate(BaseModel):
    allowed: List[Regime] = Field(default_factory=lambda: [Regime.BULLISH, Regime.NEUTRAL, Regime.BEARISH])
    min_confidence: Optional[float] = None

    @field_validator("min_confidence")
    @classmethod
    def _conf_range(cls, v: Optional[float]) -> Optional[float]:
        if v is None:
            return None
        if v < 0 or v > 1:
            raise ValueError("min_confidence must be 0..1")
        return v


class MarketHoursGate(BaseModel):
    session: Session = Session.RTH
    time_window_et: Optional[List[str]] = None  # ["HH:MM", "HH:MM"]

    @model_validator(mode="after")
    def _validate_window(self) -> "MarketHoursGate":
        if self.session == Session.CUSTOM:
            if not self.time_window_et or len(self.time_window_et) != 2:
                raise ValueError("CUSTOM session requires time_window_et=['HH:MM','HH:MM']")
        return self


class CooldownGate(BaseModel):
    seconds: int = 300

    @field_validator("seconds")
    @classmethod
    def _sec_ok(cls, v: int) -> int:
        if v < 0 or v > 86400:
            raise ValueError("cooldown seconds must be 0..86400")
        return v


class MaxTriggersGate(BaseModel):
    count: int = 3

    @field_validator("count")
    @classmethod
    def _count_ok(cls, v: int) -> int:
        if v < 1 or v > 100:
            raise ValueError("max triggers must be 1..100")
        return v


class DataFreshnessGate(BaseModel):
    price_age_seconds: int = 60

    @field_validator("price_age_seconds")
    @classmethod
    def _age_ok(cls, v: int) -> int:
        if v < 1 or v > 3600:
            raise ValueError("price_age_seconds must be 1..3600")
        return v


class Gates(BaseModel):
    regime: Optional[RegimeGate] = None
    market_hours: Optional[MarketHoursGate] = None
    cooldown: Optional[CooldownGate] = None
    max_triggers: Optional[MaxTriggersGate] = None
    data_freshness: Optional[DataFreshnessGate] = None


# -------------------------
# Lifecycle
# -------------------------


class ExpiryRelative(BaseModel):
    type: Literal["relative"] = "relative"
    minutes: Optional[int] = None
    hours: Optional[int] = None
    days: Optional[int] = None

    @model_validator(mode="after")
    def _at_least_one(self) -> "ExpiryRelative":
        if not any([self.minutes, self.hours, self.days]):
            raise ValueError("relative expiry requires minutes/hours/days")
        return self


class ExpiryDate(BaseModel):
    type: Literal["date"] = "date"
    date: date


Expiry = Union[ExpiryRelative, ExpiryDate]


class Lifecycle(BaseModel):
    start: Literal["now"] = "now"
    expires: Optional[Expiry] = None


# -------------------------
# Actions
# -------------------------


class DiscordNotifyAction(BaseModel):
    type: Literal["discord_notify"] = "discord_notify"
    style: NotifyStyle = NotifyStyle.compact


class IncludeChartAction(BaseModel):
    type: Literal["include_chart"] = "include_chart"
    chart: ChartKind = ChartKind.execution


Action = Union[DiscordNotifyAction, IncludeChartAction]


# -------------------------
# Main intent
# -------------------------


class AlertIntent(BaseModel):
    version: str = "1.0"
    source: SourceMeta
    targets: Targets
    condition: Condition
    gates: Gates = Field(default_factory=Gates)
    lifecycle: Lifecycle = Field(default_factory=Lifecycle)
    actions: List[Action] = Field(default_factory=lambda: [DiscordNotifyAction()])
    tags: Dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _caps(self) -> "AlertIntent":
        if self.targets.type == TargetsType.symbols and len(self.targets.symbols) > self.targets.max_symbols:
            raise ValueError(f"Too many symbols: {len(self.targets.symbols)} > {self.targets.max_symbols}")
        return self
