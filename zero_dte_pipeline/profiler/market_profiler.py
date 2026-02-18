"""Market condition profiler.

Analyzes recent market conditions to select appropriate
strategy presets for 0DTE trading.
"""
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from typing import Any, Dict, List, Optional

import numpy as np
import pandas as pd

from zero_dte_pipeline.candidates.scoring import Direction, Regime, Strategy
from zero_dte_pipeline.data_connectors.unified import UnifiedDataConnector
from zero_dte_pipeline.utils.logging import get_logger
from zero_dte_pipeline.utils.timeout import with_timeout

logger = get_logger(__name__)


class MarketCondition(Enum):
    """Overall market condition."""
    BULLISH_TREND = "bullish_trend"
    BEARISH_TREND = "bearish_trend"
    HIGH_VOLATILITY = "high_volatility"
    LOW_VOLATILITY = "low_volatility"
    RANGE_BOUND = "range_bound"
    UNCERTAIN = "uncertain"


@dataclass
class MarketProfile:
    """Market profile data."""
    condition: MarketCondition
    volatility_percentile: float  # 0-100
    momentum_score: float  # -1 to 1
    breadth_score: float  # 0 to 1
    recommended_strategies: List[Strategy]
    direction_bias: Direction
    regime: Regime
    confidence: float
    analysis_timestamp: datetime = field(default_factory=datetime.now)
    details: Dict[str, Any] = field(default_factory=dict)
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary."""
        return {
            "condition": self.condition.value,
            "volatility_percentile": self.volatility_percentile,
            "momentum_score": self.momentum_score,
            "breadth_score": self.breadth_score,
            "recommended_strategies": [s.value for s in self.recommended_strategies],
            "direction_bias": self.direction_bias.value,
            "regime": self.regime.value,
            "confidence": self.confidence,
            "analysis_timestamp": self.analysis_timestamp.isoformat(),
            "details": self.details,
        }


class MarketProfiler:
    """Analyzes market conditions for strategy selection.
    
    Analyzes past 10 days of:
    - Volatility (VIX, realized vol)
    - Breadth (advance/decline)
    - ES/SPX momentum
    
    Selects strategy presets:
    - Short-premium (high IV, range-bound)
    - Trend-following OTM (trending, directional)
    - Verticals (moderate IV, directional)
    - Butterflies (low IV, range-bound)
    """
    
    def __init__(
        self,
        data_connector: UnifiedDataConnector,
        lookback_days: int = 10,
    ):
        self.data_connector = data_connector
        self.lookback_days = lookback_days
        self._last_profile: Optional[MarketProfile] = None
    
    async def analyze(self) -> MarketProfile:
        """Perform full market analysis.
        
        Returns:
            MarketProfile with analysis results.
        """
        end = datetime.now()
        start = end - timedelta(days=self.lookback_days)
        
        # Fetch data for analysis
        spx_data = await with_timeout(
            self.data_connector.get_historical_bars("SPY", start, end, "1d"),
            timeout=30,
            default=None,
            operation_name="get_spx_history",
        )
        
        vix_data = await with_timeout(
            self.data_connector.get_historical_bars("VIX", start, end, "1d"),
            timeout=30,
            default=None,
            operation_name="get_vix_history",
        )
        
        # Calculate metrics
        volatility = self._calculate_volatility(spx_data, vix_data)
        momentum = self._calculate_momentum(spx_data)
        breadth = await self._calculate_breadth()
        
        # Determine condition and regime
        condition = self._determine_condition(volatility, momentum)
        regime = self._determine_regime(volatility, condition)
        direction = self._determine_direction(momentum)
        
        # Select strategies
        strategies = self._select_strategies(condition, volatility, momentum)
        
        # Calculate confidence
        confidence = self._calculate_confidence(volatility, momentum, breadth)
        
        profile = MarketProfile(
            condition=condition,
            volatility_percentile=volatility.get("percentile", 50),
            momentum_score=momentum.get("score", 0),
            breadth_score=breadth.get("score", 0.5),
            recommended_strategies=strategies,
            direction_bias=direction,
            regime=regime,
            confidence=confidence,
            details={
                "volatility": volatility,
                "momentum": momentum,
                "breadth": breadth,
            },
        )
        
        self._last_profile = profile
        logger.info(f"Market profile: {condition.value}, regime: {regime.value}")
        
        return profile
    
    def _calculate_volatility(
        self,
        spx_data: Optional[pd.DataFrame],
        vix_data: Optional[pd.DataFrame],
    ) -> Dict[str, Any]:
        """Calculate volatility metrics.
        
        Args:
            spx_data: SPX/SPY historical data
            vix_data: VIX historical data
            
        Returns:
            Dictionary with volatility metrics.
        """
        result = {
            "realized": None,
            "vix_level": None,
            "percentile": 50,
            "is_elevated": False,
        }
        
        if spx_data is not None and not spx_data.empty:
            # Calculate realized volatility (annualized)
            returns = spx_data["close"].pct_change().dropna()
            realized_vol = returns.std() * np.sqrt(252) * 100
            result["realized"] = float(realized_vol)
            
            # Calculate where current vol sits historically
            rolling_vol = returns.rolling(5).std() * np.sqrt(252) * 100
            current_vol = rolling_vol.iloc[-1] if not rolling_vol.empty else realized_vol
            
            # Simple percentile calculation
            if len(rolling_vol) > 5:
                percentile = (rolling_vol < current_vol).mean() * 100
                result["percentile"] = float(percentile)
        
        if vix_data is not None and not vix_data.empty:
            vix_level = vix_data["close"].iloc[-1]
            result["vix_level"] = float(vix_level)
            result["is_elevated"] = vix_level > 20
            
            # Adjust percentile based on VIX
            if vix_level > 30:
                result["percentile"] = max(result["percentile"], 80)
            elif vix_level > 25:
                result["percentile"] = max(result["percentile"], 70)
            elif vix_level < 15:
                result["percentile"] = min(result["percentile"], 30)
        
        return result
    
    def _calculate_momentum(
        self,
        spx_data: Optional[pd.DataFrame],
    ) -> Dict[str, Any]:
        """Calculate momentum metrics.
        
        Args:
            spx_data: SPX/SPY historical data
            
        Returns:
            Dictionary with momentum metrics.
        """
        result = {
            "score": 0.0,
            "trend": "neutral",
            "strength": 0.0,
            "ma_position": None,
        }
        
        if spx_data is None or spx_data.empty:
            return result
        
        close = spx_data["close"]
        
        # Calculate momentum score (-1 to 1)
        if len(close) >= 5:
            short_return = (close.iloc[-1] / close.iloc[-5] - 1) * 100
            result["short_return"] = float(short_return)
        
        if len(close) >= 10:
            returns = close.pct_change().dropna()
            
            # Simple momentum score based on recent returns
            recent_return = (close.iloc[-1] / close.iloc[0] - 1) * 100
            
            # Normalize to -1 to 1 range (assume ±5% is extreme)
            score = np.clip(recent_return / 5, -1, 1)
            result["score"] = float(score)
            
            # Determine trend
            if score > 0.3:
                result["trend"] = "bullish"
            elif score < -0.3:
                result["trend"] = "bearish"
            else:
                result["trend"] = "neutral"
            
            result["strength"] = abs(score)
            
            # MA position (price relative to moving average)
            ma_10 = close.rolling(10).mean().iloc[-1]
            result["ma_position"] = float((close.iloc[-1] / ma_10 - 1) * 100)
        
        return result
    
    async def _calculate_breadth(self) -> Dict[str, Any]:
        """Calculate market breadth metrics.
        
        Returns:
            Dictionary with breadth metrics.
        """
        # Simplified breadth calculation
        # In a full implementation, this would use advance/decline data
        result = {
            "score": 0.5,  # Default neutral
            "advance_decline_ratio": None,
            "new_highs_lows": None,
        }
        
        # Use QQQ vs SPY ratio as a breadth proxy
        end = datetime.now()
        start = end - timedelta(days=10)
        
        spy_data = await with_timeout(
            self.data_connector.get_historical_bars("SPY", start, end, "1d"),
            timeout=15,
            default=None,
        )
        
        qqq_data = await with_timeout(
            self.data_connector.get_historical_bars("QQQ", start, end, "1d"),
            timeout=15,
            default=None,
        )
        
        iwm_data = await with_timeout(
            self.data_connector.get_historical_bars("IWM", start, end, "1d"),
            timeout=15,
            default=None,
        )
        
        # Calculate breadth from relative performance
        scores = []
        
        if spy_data is not None and not spy_data.empty:
            spy_return = spy_data["close"].iloc[-1] / spy_data["close"].iloc[0] - 1
            scores.append(0.5 + spy_return * 5)  # Convert return to 0-1 score
        
        if qqq_data is not None and not qqq_data.empty:
            qqq_return = qqq_data["close"].iloc[-1] / qqq_data["close"].iloc[0] - 1
            scores.append(0.5 + qqq_return * 5)
        
        if iwm_data is not None and not iwm_data.empty:
            iwm_return = iwm_data["close"].iloc[-1] / iwm_data["close"].iloc[0] - 1
            scores.append(0.5 + iwm_return * 5)
        
        if scores:
            # High score if all indices are moving together positively
            # Low score if divergence or negative
            avg_score = np.mean(scores)
            score_std = np.std(scores) if len(scores) > 1 else 0
            
            # Penalize divergence
            result["score"] = float(np.clip(avg_score - score_std, 0, 1))
        
        return result
    
    def _determine_condition(
        self,
        volatility: Dict[str, Any],
        momentum: Dict[str, Any],
    ) -> MarketCondition:
        """Determine overall market condition.
        
        Args:
            volatility: Volatility metrics
            momentum: Momentum metrics
            
        Returns:
            MarketCondition enum value.
        """
        vol_percentile = volatility.get("percentile", 50)
        mom_score = momentum.get("score", 0)
        mom_strength = momentum.get("strength", 0)
        
        # High volatility conditions
        if vol_percentile > 75:
            return MarketCondition.HIGH_VOLATILITY
        
        # Low volatility conditions
        if vol_percentile < 25:
            return MarketCondition.LOW_VOLATILITY
        
        # Trending conditions
        if mom_strength > 0.5:
            if mom_score > 0:
                return MarketCondition.BULLISH_TREND
            else:
                return MarketCondition.BEARISH_TREND
        
        # Range-bound or uncertain
        if mom_strength < 0.2:
            return MarketCondition.RANGE_BOUND
        
        return MarketCondition.UNCERTAIN
    
    def _determine_regime(
        self,
        volatility: Dict[str, Any],
        condition: MarketCondition,
    ) -> Regime:
        """Determine market regime.
        
        Args:
            volatility: Volatility metrics
            condition: Market condition
            
        Returns:
            Regime enum value.
        """
        vol_percentile = volatility.get("percentile", 50)
        
        if vol_percentile > 70:
            return Regime.HIGH_VOL
        elif vol_percentile < 30:
            return Regime.LOW_VOL
        elif condition in [MarketCondition.BULLISH_TREND, MarketCondition.BEARISH_TREND]:
            return Regime.TRENDING
        elif condition == MarketCondition.RANGE_BOUND:
            return Regime.RANGING
        else:
            return Regime.UNKNOWN
    
    def _determine_direction(self, momentum: Dict[str, Any]) -> Direction:
        """Determine direction bias.
        
        Args:
            momentum: Momentum metrics
            
        Returns:
            Direction enum value.
        """
        score = momentum.get("score", 0)
        
        if score > 0.2:
            return Direction.BULLISH
        elif score < -0.2:
            return Direction.BEARISH
        else:
            return Direction.NEUTRAL
    
    def _select_strategies(
        self,
        condition: MarketCondition,
        volatility: Dict[str, Any],
        momentum: Dict[str, Any],
    ) -> List[Strategy]:
        """Select recommended strategies based on conditions.
        
        Args:
            condition: Market condition
            volatility: Volatility metrics
            momentum: Momentum metrics
            
        Returns:
            List of recommended strategies.
        """
        strategies = []
        vol_percentile = volatility.get("percentile", 50)
        
        # High volatility: short premium strategies
        if condition == MarketCondition.HIGH_VOLATILITY or vol_percentile > 65:
            strategies.extend([Strategy.SHORT_PREMIUM, Strategy.IRON_CONDOR])
        
        # Trending: directional strategies
        if condition in [MarketCondition.BULLISH_TREND, MarketCondition.BEARISH_TREND]:
            strategies.extend([Strategy.TREND_FOLLOWING, Strategy.VERTICAL])
        
        # Low volatility: defined risk, directional
        if condition == MarketCondition.LOW_VOLATILITY or vol_percentile < 35:
            strategies.extend([Strategy.BUTTERFLY, Strategy.VERTICAL])
        
        # Range-bound: neutral strategies
        if condition == MarketCondition.RANGE_BOUND:
            strategies.extend([Strategy.BUTTERFLY, Strategy.IRON_CONDOR])
        
        # Ensure at least one strategy
        if not strategies:
            strategies = [Strategy.VERTICAL, Strategy.SHORT_PREMIUM]
        
        # Remove duplicates while preserving order
        seen = set()
        unique = []
        for s in strategies:
            if s not in seen:
                seen.add(s)
                unique.append(s)
        
        return unique
    
    def _calculate_confidence(
        self,
        volatility: Dict[str, Any],
        momentum: Dict[str, Any],
        breadth: Dict[str, Any],
    ) -> float:
        """Calculate confidence in the analysis.
        
        Args:
            volatility: Volatility metrics
            momentum: Momentum metrics
            breadth: Breadth metrics
            
        Returns:
            Confidence score from 0 to 1.
        """
        scores = []
        
        # Data availability score
        if volatility.get("realized") is not None:
            scores.append(0.8)
        else:
            scores.append(0.3)
        
        if volatility.get("vix_level") is not None:
            scores.append(0.9)
        else:
            scores.append(0.4)
        
        # Momentum clarity score
        mom_strength = momentum.get("strength", 0)
        scores.append(0.5 + mom_strength * 0.5)
        
        # Breadth confirmation
        breadth_score = breadth.get("score", 0.5)
        scores.append(breadth_score)
        
        return float(np.mean(scores))
    
    @property
    def last_profile(self) -> Optional[MarketProfile]:
        """Get the last calculated profile."""
        return self._last_profile
