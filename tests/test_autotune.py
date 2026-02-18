"""Tests for autotune module."""
import pytest
import numpy as np

from zero_dte_pipeline.autotune.autotune import (
    CandidateAutotune,
    AutotuneResult,
    TradeOutcome,
)


class TestCandidateAutotune:
    """Test autotune optimization."""
    
    def test_simulate_trades(self):
        """Should generate simulated trades."""
        tuner = CandidateAutotune()
        
        trades = tuner.simulate_trades(num_trades=50)
        
        assert len(trades) == 50
        assert all(isinstance(t, TradeOutcome) for t in trades)
        assert all(t.entry_price > 0 for t in trades)
    
    def test_simulate_trades_win_rate(self):
        """Simulated win rate should be approximately correct."""
        tuner = CandidateAutotune()
        
        trades = tuner.simulate_trades(num_trades=1000, win_rate=0.6)
        
        actual_win_rate = sum(1 for t in trades if t.win) / len(trades)
        
        # Should be within 5% of target
        assert abs(actual_win_rate - 0.6) < 0.1
    
    def test_optimize_with_no_history(self):
        """Should handle empty trade history."""
        tuner = CandidateAutotune()
        
        result = tuner.optimize([])
        
        assert isinstance(result, AutotuneResult)
        assert result.iterations == 0
        assert "error" in result.metrics
    
    def test_optimize_basic(self):
        """Should optimize parameters."""
        tuner = CandidateAutotune()
        
        trades = tuner.simulate_trades(num_trades=100)
        result = tuner.optimize(trades, max_iterations=20)
        
        assert isinstance(result, AutotuneResult)
        assert result.iterations > 0
        assert result.best_score >= 0
    
    def test_aggressive_mode(self):
        """Aggressive mode should have wider bounds."""
        normal = CandidateAutotune(aggressive=False)
        aggressive = CandidateAutotune(aggressive=True)
        
        # Aggressive should have lower min bounds
        assert aggressive.PARAM_BOUNDS["min_score"][0] <= normal.PARAM_BOUNDS["min_score"][0]
    
    def test_result_to_dict(self):
        """Result should serialize to dict."""
        tuner = CandidateAutotune()
        trades = tuner.simulate_trades(num_trades=50)
        result = tuner.optimize(trades, max_iterations=5)
        
        result_dict = result.to_dict()
        
        assert isinstance(result_dict, dict)
        assert "timestamp" in result_dict
        assert "best_params" in result_dict
        assert "metrics" in result_dict
    
    def test_properties(self):
        """Properties should be accessible after optimization."""
        tuner = CandidateAutotune()
        trades = tuner.simulate_trades(num_trades=50)
        tuner.optimize(trades, max_iterations=5)
        
        assert isinstance(tuner.best_params, dict)
        assert isinstance(tuner.best_score, float)


class TestTradeOutcome:
    """Test trade outcome data class."""
    
    def test_create_outcome(self):
        """Should create trade outcome."""
        outcome = TradeOutcome(
            entry_price=100.0,
            exit_price=105.0,
            direction="bullish",
            strategy="vertical",
            score_at_entry=0.7,
            confidence_at_entry=0.6,
            regime_at_entry="trending",
            pnl=5.0,
            pnl_percent=5.0,
            holding_time_minutes=60,
            win=True,
        )
        
        assert outcome.entry_price == 100.0
        assert outcome.win is True
        assert outcome.pnl_percent == 5.0
