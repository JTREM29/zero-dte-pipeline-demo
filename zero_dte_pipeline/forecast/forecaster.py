"""Intraday directional forecast utilities.

Produces short-horizon signals combining recent price action,
market profile context, and options surface data.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from typing import Any, Dict, List, Optional

import numpy as np
import pandas as pd

from zero_dte_pipeline.candidates.scoring import Direction
from zero_dte_pipeline.data_connectors.unified import UnifiedDataConnector
from zero_dte_pipeline.utils.logging import get_logger
from zero_dte_pipeline.utils.timeout import with_timeout

logger = get_logger(__name__)


class ForecastConfidence(Enum):
    """Qualitative confidence bucket."""
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


@dataclass
class ForecastComponent:
    """Individual component contribution."""
    name: str
    score: float
    weight: float
    rationale: str

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "score": self.score,
            "weight": self.weight,
            "rationale": self.rationale,
        }


@dataclass
class ForecastResult:
    """Aggregated forecast output."""
    symbol: str
    timestamp: datetime
    horizon_minutes: int
    direction: Direction
    score: float  # -1 bearish, +1 bullish
    confidence: ForecastConfidence
    expected_move_pct: float
    components: List[ForecastComponent] = field(default_factory=list)
    technical_levels: Dict[str, float] = field(default_factory=dict)
    context: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "symbol": self.symbol,
            "timestamp": self.timestamp.isoformat(),
            "horizon_minutes": self.horizon_minutes,
            "direction": self.direction.value,
            "score": self.score,
            "confidence": self.confidence.value,
            "expected_move_pct": self.expected_move_pct,
            "components": [c.to_dict() for c in self.components],
            "technical_levels": self.technical_levels,
            "context": self.context,
        }


class Forecaster:
    """Combines market data into a directional forecast."""

    SYMBOL_FALLBACKS = {
        "SPX": ["SPX", "SPY"],
        "NDX": ["NDX", "QQQ"],
        "DJX": ["DJX", "DIA"],
    }

    def __init__(
        self,
        connector: UnifiedDataConnector,
        horizon_minutes: int = 60,
        lookback_minutes: int = 360,
    ):
        self.connector = connector
        self.horizon_minutes = horizon_minutes
        self.lookback_minutes = lookback_minutes

    async def forecast(self, symbol: str) -> ForecastResult:
        """Compute directional forecast for a symbol."""
        now = datetime.now()
        start = now - timedelta(minutes=self.lookback_minutes)

        history, source_symbol = await self._fetch_history(symbol, start, now)

        if history is None or history.empty:
            raise RuntimeError(f"No historical bars available for {symbol}")

        history = history.sort_index()
        latest = history.iloc[-1]

        components: List[ForecastComponent] = []

        momentum = self._calc_momentum(history)
        components.append(
            ForecastComponent(
                name="momentum",
                score=momentum,
                weight=0.45,
                rationale="Slope of recent 5m closes",
            )
        )

        mean_reversion = self._calc_mean_reversion(history)
        components.append(
            ForecastComponent(
                name="mean_reversion",
                score=mean_reversion,
                weight=0.25,
                rationale="Distance from intraday VWAP",
            )
        )

        volatility = self._calc_volatility(history)
        components.append(
            ForecastComponent(
                name="volatility",
                score=volatility,
                weight=0.15,
                rationale="Normalized ATR regime",
            )
        )

        breadth = await self._calc_breadth_proxy(now)
        components.append(
            ForecastComponent(
                name="breadth",
                score=breadth,
                weight=0.15,
                rationale="QQQ vs IWM relative strength",
            )
        )

        score = float(
            np.clip(
                sum(c.score * c.weight for c in components),
                -1.0,
                1.0,
            )
        )
        direction = self._score_to_direction(score)
        confidence = self._score_to_confidence(score, history)

        expected_move = float((history["close"].diff().rolling(12).std().iloc[-1] or 0) * 100)

        technical_levels = {
            "last": float(latest["close"]),
            "vwap": float(history["vwap"].dropna().iloc[-1] if "vwap" in history else latest["close"]),
            "support": float(history["low"].tail(12).min()),
            "resistance": float(history["high"].tail(12).max()),
        }

        context = {
            "bars_analyzed": len(history),
            "lookback_minutes": self.lookback_minutes,
            "source_symbol": source_symbol,
        }

        return ForecastResult(
            symbol=symbol,
            timestamp=now,
            horizon_minutes=self.horizon_minutes,
            direction=direction,
            score=score,
            confidence=confidence,
            expected_move_pct=expected_move,
            components=components,
            technical_levels=technical_levels,
            context=context,
        )

    def _calc_momentum(self, history: pd.DataFrame) -> float:
        closes = history["close"]
        if len(closes) < 5:
            return 0.0
        slope = (closes.iloc[-1] - closes.iloc[-5]) / closes.iloc[-5]
        return float(np.clip(slope * 5, -1, 1))

    def _calc_mean_reversion(self, history: pd.DataFrame) -> float:
        if "vwap" not in history or history["vwap"].isna().all():
            return 0.0
        price = history["close"].iloc[-1]
        vwap = history["vwap"].dropna().iloc[-1]
        dist = (price - vwap) / vwap
        return float(np.clip(-dist * 4, -1, 1))

    def _calc_volatility(self, history: pd.DataFrame) -> float:
        returns = history["close"].pct_change().dropna()
        if returns.empty:
            return 0.0
        atr_like = history["high"] - history["low"]
        atr_norm = (atr_like.rolling(6).mean() / history["close"]).iloc[-1]
        return float(np.clip((0.03 - atr_norm) * 10, -1, 1))

    async def _fetch_history(
        self,
        symbol: str,
        start: datetime,
        end: datetime,
    ) -> tuple[Optional[pd.DataFrame], Optional[str]]:
        """Fetch intraday history with logical fallbacks."""
        candidates = self.SYMBOL_FALLBACKS.get(symbol.upper(), [symbol])
        for candidate in candidates:
            history = await with_timeout(
                self.connector.get_historical_bars(
                    candidate,
                    start,
                    end,
                    timeframe="5m",
                    providers=["polygon"],
                ),
                timeout=30,
                default=None,
                operation_name=f"forecast_history_{candidate}",
            )

            if history is not None and not history.empty:
                if candidate != symbol:
                    logger.info(
                        "Forecast using fallback symbol %s for %s",
                        candidate,
                        symbol,
                    )
                return history, candidate

        return None, None

    async def _calc_breadth_proxy(self, now: datetime) -> float:
        start = now - timedelta(minutes=self.lookback_minutes)
        qqq = await with_timeout(
            self.connector.get_historical_bars(
                "QQQ",
                start,
                now,
                timeframe="5m",
                providers=["polygon"],
            ),
            timeout=20,
            default=None,
        )
        iwm = await with_timeout(
            self.connector.get_historical_bars(
                "IWM",
                start,
                now,
                timeframe="5m",
                providers=["polygon"],
            ),
            timeout=20,
            default=None,
        )
        if qqq is None or qqq.empty or iwm is None or iwm.empty:
            return 0.0
        qqq_ret = qqq["close"].iloc[-1] / qqq["close"].iloc[0] - 1
        iwm_ret = iwm["close"].iloc[-1] / iwm["close"].iloc[0] - 1
        return float(np.clip((qqq_ret - iwm_ret) * 5, -1, 1))

    def _score_to_direction(self, score: float) -> Direction:
        if score > 0.1:
            return Direction.BULLISH
        if score < -0.1:
            return Direction.BEARISH
        return Direction.NEUTRAL

    def _score_to_confidence(self, score: float, history: pd.DataFrame) -> ForecastConfidence:
        magnitude = abs(score)
        if magnitude > 0.6:
            return ForecastConfidence.HIGH
        if magnitude > 0.3:
            return ForecastConfidence.MEDIUM
        return ForecastConfidence.LOW