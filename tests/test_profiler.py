"""Tests for market profiler."""
import pytest
from datetime import datetime

from zero_dte_pipeline.profiler.market_profiler import (
    MarketProfiler,
    MarketProfile,
    MarketCondition,
)
from zero_dte_pipeline.candidates.scoring import Direction, Regime, Strategy


class TestMarketProfile:
    """Test market profile data class."""
    
    def test_create_profile(self):
        """Should create market profile."""
        profile = MarketProfile(
            condition=MarketCondition.BULLISH_TREND,
            volatility_percentile=60.0,
            momentum_score=0.5,
            breadth_score=0.7,
            recommended_strategies=[Strategy.TREND_FOLLOWING, Strategy.VERTICAL],
            direction_bias=Direction.BULLISH,
            regime=Regime.TRENDING,
            confidence=0.8,
        )
        
        assert profile.condition == MarketCondition.BULLISH_TREND
        assert profile.volatility_percentile == 60.0
        assert profile.confidence == 0.8
    
    def test_to_dict(self):
        """Should serialize to dictionary."""
        profile = MarketProfile(
            condition=MarketCondition.HIGH_VOLATILITY,
            volatility_percentile=85.0,
            momentum_score=-0.2,
            breadth_score=0.4,
            recommended_strategies=[Strategy.SHORT_PREMIUM],
            direction_bias=Direction.BEARISH,
            regime=Regime.HIGH_VOL,
            confidence=0.7,
        )
        
        result = profile.to_dict()
        
        assert isinstance(result, dict)
        assert result["condition"] == "high_volatility"
        assert result["direction_bias"] == "bearish"
        assert result["regime"] == "high_volatility"
        assert "recommended_strategies" in result


class TestMarketProfilerConditions:
    """Test market profiler condition determination."""
    
    def test_high_volatility_detection(self):
        """Should detect high volatility condition."""
        volatility = {"percentile": 85, "vix_level": 30}
        momentum = {"score": 0.1, "strength": 0.1}
        
        profiler = MarketProfiler.__new__(MarketProfiler)
        condition = profiler._determine_condition(volatility, momentum)
        
        assert condition == MarketCondition.HIGH_VOLATILITY
    
    def test_low_volatility_detection(self):
        """Should detect low volatility condition."""
        volatility = {"percentile": 15, "vix_level": 12}
        momentum = {"score": 0.1, "strength": 0.1}
        
        profiler = MarketProfiler.__new__(MarketProfiler)
        condition = profiler._determine_condition(volatility, momentum)
        
        assert condition == MarketCondition.LOW_VOLATILITY
    
    def test_bullish_trend_detection(self):
        """Should detect bullish trend."""
        volatility = {"percentile": 50}
        momentum = {"score": 0.7, "strength": 0.7}
        
        profiler = MarketProfiler.__new__(MarketProfiler)
        condition = profiler._determine_condition(volatility, momentum)
        
        assert condition == MarketCondition.BULLISH_TREND
    
    def test_bearish_trend_detection(self):
        """Should detect bearish trend."""
        volatility = {"percentile": 50}
        momentum = {"score": -0.7, "strength": 0.7}
        
        profiler = MarketProfiler.__new__(MarketProfiler)
        condition = profiler._determine_condition(volatility, momentum)
        
        assert condition == MarketCondition.BEARISH_TREND
    
    def test_range_bound_detection(self):
        """Should detect range-bound market."""
        volatility = {"percentile": 50}
        momentum = {"score": 0.05, "strength": 0.05}
        
        profiler = MarketProfiler.__new__(MarketProfiler)
        condition = profiler._determine_condition(volatility, momentum)
        
        assert condition == MarketCondition.RANGE_BOUND


class TestMarketProfilerStrategies:
    """Test strategy selection."""
    
    def test_high_vol_strategies(self):
        """High vol should recommend short premium."""
        profiler = MarketProfiler.__new__(MarketProfiler)
        
        strategies = profiler._select_strategies(
            MarketCondition.HIGH_VOLATILITY,
            {"percentile": 80},
            {"score": 0.1},
        )
        
        assert Strategy.SHORT_PREMIUM in strategies
    
    def test_trending_strategies(self):
        """Trending should recommend directional."""
        profiler = MarketProfiler.__new__(MarketProfiler)
        
        strategies = profiler._select_strategies(
            MarketCondition.BULLISH_TREND,
            {"percentile": 50},
            {"score": 0.6},
        )
        
        assert Strategy.TREND_FOLLOWING in strategies or Strategy.VERTICAL in strategies
    
    def test_low_vol_strategies(self):
        """Low vol should recommend butterflies."""
        profiler = MarketProfiler.__new__(MarketProfiler)
        
        strategies = profiler._select_strategies(
            MarketCondition.LOW_VOLATILITY,
            {"percentile": 20},
            {"score": 0.1},
        )
        
        assert Strategy.BUTTERFLY in strategies


class TestMarketProfilerDirection:
    """Test direction determination."""
    
    def test_bullish_direction(self):
        """Positive momentum should be bullish."""
        profiler = MarketProfiler.__new__(MarketProfiler)
        
        direction = profiler._determine_direction({"score": 0.5})
        
        assert direction == Direction.BULLISH
    
    def test_bearish_direction(self):
        """Negative momentum should be bearish."""
        profiler = MarketProfiler.__new__(MarketProfiler)
        
        direction = profiler._determine_direction({"score": -0.5})
        
        assert direction == Direction.BEARISH
    
    def test_neutral_direction(self):
        """Neutral momentum should be neutral."""
        profiler = MarketProfiler.__new__(MarketProfiler)
        
        direction = profiler._determine_direction({"score": 0.05})
        
        assert direction == Direction.NEUTRAL
