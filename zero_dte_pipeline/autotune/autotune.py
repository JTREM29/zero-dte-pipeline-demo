"""Candidate autotune module.

Optimizes candidate scoring and gating parameters based on
historical performance.
"""
import json
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import numpy as np

from zero_dte_pipeline.candidates.gating import GatingManager
from zero_dte_pipeline.candidates.scoring import CandidateScorer
from zero_dte_pipeline.config import config
from zero_dte_pipeline.utils.logging import get_logger

logger = get_logger(__name__)


@dataclass
class AutotuneResult:
    """Result of autotune optimization."""
    timestamp: datetime
    iterations: int
    best_params: Dict[str, Any]
    best_score: float
    improvement: float
    metrics: Dict[str, Any]
    config_updated: bool
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary."""
        return {
            "timestamp": self.timestamp.isoformat(),
            "iterations": self.iterations,
            "best_params": self.best_params,
            "best_score": self.best_score,
            "improvement": self.improvement,
            "metrics": self.metrics,
            "config_updated": self.config_updated,
        }


@dataclass
class TradeOutcome:
    """Historical trade outcome for optimization."""
    entry_price: float
    exit_price: float
    direction: str
    strategy: str
    score_at_entry: float
    confidence_at_entry: float
    regime_at_entry: str
    pnl: float
    pnl_percent: float
    holding_time_minutes: int
    win: bool


class CandidateAutotune:
    """Optimizes candidate scoring and gating parameters.
    
    Features:
    - Grid search optimization
    - Performance-based tuning
    - Safe parameter bounds
    - Config file updates
    """
    
    # Parameter bounds (min, max, step)
    PARAM_BOUNDS = {
        "min_score": (0.15, 0.6, 0.05),
        "min_confidence": (0.2, 0.6, 0.05),
        "direction_weight": (0.15, 0.45, 0.05),
        "regime_weight": (0.1, 0.35, 0.05),
        "iv_weight": (0.1, 0.4, 0.05),
        "order_flow_weight": (0.1, 0.4, 0.05),
        "iv_threshold": (0.1, 0.4, 0.05),
    }
    
    # Gating parameter bounds
    GATING_BOUNDS = {
        "min_score": (0.15, 0.55, 0.05),
        "min_confidence": (0.2, 0.55, 0.05),
        "max_daily_trades": (3, 25, 2),
        "max_concurrent_positions": (2, 12, 1),
    }
    
    def __init__(
        self,
        config_path: Optional[Path] = None,
        aggressive: bool = False,
    ):
        """Initialize autotune.
        
        Args:
            config_path: Path to config file for updates
            aggressive: Whether to use more aggressive optimization
        """
        self.config_path = config_path
        self.aggressive = aggressive
        
        # Adjust bounds for aggressive mode
        if aggressive:
            self.PARAM_BOUNDS = {
                "min_score": (0.1, 0.5, 0.05),
                "min_confidence": (0.15, 0.5, 0.05),
                "direction_weight": (0.1, 0.5, 0.05),
                "regime_weight": (0.05, 0.4, 0.05),
                "iv_weight": (0.1, 0.45, 0.05),
                "order_flow_weight": (0.1, 0.45, 0.05),
                "iv_threshold": (0.05, 0.45, 0.05),
            }
            self.GATING_BOUNDS = {
                "min_score": (0.1, 0.45, 0.05),
                "min_confidence": (0.15, 0.45, 0.05),
                "max_daily_trades": (5, 30, 3),
                "max_concurrent_positions": (3, 15, 2),
            }
        
        # Current best parameters
        self._best_params: Dict[str, Any] = {}
        self._best_score: float = 0.0
        self._baseline_score: float = 0.0
    
    def optimize(
        self,
        trade_history: List[TradeOutcome],
        max_iterations: int = 100,
        early_stop_threshold: float = 0.01,
    ) -> AutotuneResult:
        """Run optimization on trade history.
        
        Args:
            trade_history: Historical trade outcomes
            max_iterations: Maximum optimization iterations
            early_stop_threshold: Stop if improvement below this
            
        Returns:
            AutotuneResult with optimized parameters.
        """
        if not trade_history:
            logger.warning("No trade history provided for optimization")
            return AutotuneResult(
                timestamp=datetime.now(),
                iterations=0,
                best_params={},
                best_score=0.0,
                improvement=0.0,
                metrics={"error": "No trade history"},
                config_updated=False,
            )
        
        # Calculate baseline score with current params
        self._baseline_score = self._calculate_score(trade_history, {})
        logger.info(f"Baseline score: {self._baseline_score:.4f}")
        
        # Grid search optimization
        best_params, best_score, iterations = self._grid_search(
            trade_history,
            max_iterations,
            early_stop_threshold,
        )
        
        improvement = (best_score - self._baseline_score) / max(self._baseline_score, 0.001)
        
        # Update config if improvement is significant
        config_updated = False
        if improvement > 0.05:  # 5% improvement threshold
            config_updated = self._update_config(best_params)
        
        self._best_params = best_params
        self._best_score = best_score
        
        return AutotuneResult(
            timestamp=datetime.now(),
            iterations=iterations,
            best_params=best_params,
            best_score=best_score,
            improvement=improvement,
            metrics={
                "baseline_score": self._baseline_score,
                "trade_count": len(trade_history),
                "win_rate": sum(1 for t in trade_history if t.win) / len(trade_history),
                "avg_pnl": np.mean([t.pnl_percent for t in trade_history]),
            },
            config_updated=config_updated,
        )
    
    def _grid_search(
        self,
        trade_history: List[TradeOutcome],
        max_iterations: int,
        early_stop_threshold: float,
    ) -> Tuple[Dict[str, Any], float, int]:
        """Perform grid search optimization.
        
        Args:
            trade_history: Historical trades
            max_iterations: Max iterations
            early_stop_threshold: Early stopping threshold
            
        Returns:
            Tuple of (best_params, best_score, iterations).
        """
        best_params: Dict[str, Any] = {}
        best_score = self._baseline_score
        iterations = 0
        no_improvement_count = 0
        
        # Generate parameter combinations
        param_combinations = self._generate_param_combinations()
        
        for params in param_combinations:
            if iterations >= max_iterations:
                break
            
            score = self._calculate_score(trade_history, params)
            iterations += 1
            
            if score > best_score:
                improvement = (score - best_score) / max(best_score, 0.001)
                best_score = score
                best_params = params.copy()
                no_improvement_count = 0
                
                logger.info(f"New best score: {best_score:.4f} (improvement: {improvement:.2%})")
            else:
                no_improvement_count += 1
            
            # Early stopping
            if no_improvement_count > 20:
                logger.info(f"Early stopping after {iterations} iterations")
                break
        
        return best_params, best_score, iterations
    
    def _generate_param_combinations(self) -> List[Dict[str, Any]]:
        """Generate parameter combinations for grid search.
        
        Returns:
            List of parameter dictionaries.
        """
        combinations = []
        
        # Start with scoring parameters
        for min_score in np.arange(*self.PARAM_BOUNDS["min_score"]):
            for min_conf in np.arange(*self.PARAM_BOUNDS["min_confidence"]):
                for dir_weight in np.arange(*self.PARAM_BOUNDS["direction_weight"]):
                    # Ensure weights sum approximately to 1
                    remaining = 1.0 - dir_weight
                    regime_weight = remaining * 0.25
                    iv_weight = remaining * 0.35
                    of_weight = remaining * 0.40
                    
                    combinations.append({
                        "scoring": {
                            "min_score": float(min_score),
                            "min_confidence": float(min_conf),
                            "direction_weight": float(dir_weight),
                            "regime_weight": float(regime_weight),
                            "iv_weight": float(iv_weight),
                            "order_flow_weight": float(of_weight),
                        },
                        "gating": {
                            "min_score": float(min_score),
                            "min_confidence": float(min_conf),
                        },
                    })
        
        # Shuffle for random exploration
        np.random.shuffle(combinations)
        
        return combinations
    
    def _calculate_score(
        self,
        trade_history: List[TradeOutcome],
        params: Dict[str, Any],
    ) -> float:
        """Calculate optimization score for parameters.
        
        The score balances:
        - Win rate
        - Average PnL
        - Sharpe-like ratio
        - Trade frequency
        
        Args:
            trade_history: Historical trades
            params: Parameters to evaluate
            
        Returns:
            Optimization score.
        """
        if not trade_history:
            return 0.0
        
        # Simulate filtering with these parameters
        scoring_params = params.get("scoring", {})
        gating_params = params.get("gating", {})
        
        min_score = gating_params.get("min_score", 0.35)
        min_confidence = gating_params.get("min_confidence", 0.35)
        
        # Filter trades that would have passed
        passed_trades = [
            t for t in trade_history
            if t.score_at_entry >= min_score and t.confidence_at_entry >= min_confidence
        ]
        
        if not passed_trades:
            return 0.0
        
        # Calculate metrics
        win_rate = sum(1 for t in passed_trades if t.win) / len(passed_trades)
        avg_pnl = np.mean([t.pnl_percent for t in passed_trades])
        pnl_std = np.std([t.pnl_percent for t in passed_trades]) or 1
        
        # Sharpe-like ratio
        sharpe = avg_pnl / pnl_std if pnl_std > 0 else 0
        
        # Trade frequency factor (penalize if too few trades)
        freq_factor = min(1.0, len(passed_trades) / (len(trade_history) * 0.3))
        
        # Combined score
        score = (
            win_rate * 0.30 +
            (avg_pnl / 10) * 0.30 +  # Normalize avg_pnl
            max(0, sharpe) * 0.25 +
            freq_factor * 0.15
        )
        
        return float(score)
    
    def _update_config(self, params: Dict[str, Any]) -> bool:
        """Update configuration file with optimized parameters.
        
        Args:
            params: Optimized parameters
            
        Returns:
            True if config was updated.
        """
        if not self.config_path:
            logger.info("No config path specified, skipping config update")
            return False
        
        try:
            # Load existing config
            if self.config_path.exists():
                with open(self.config_path, "r") as f:
                    existing = json.load(f)
            else:
                existing = {}
            
            # Update with new parameters
            existing["autotune"] = {
                "last_updated": datetime.now().isoformat(),
                "scoring": params.get("scoring", {}),
                "gating": params.get("gating", {}),
            }
            
            # Write back
            with open(self.config_path, "w") as f:
                json.dump(existing, f, indent=2)
            
            logger.info(f"Updated config at {self.config_path}")
            return True
            
        except Exception as e:
            logger.error(f"Failed to update config: {e}")
            return False
    
    def simulate_trades(
        self,
        num_trades: int = 100,
        win_rate: float = 0.55,
        avg_win: float = 5.0,
        avg_loss: float = -3.0,
    ) -> List[TradeOutcome]:
        """Generate simulated trade history for testing.
        
        Args:
            num_trades: Number of trades to simulate
            win_rate: Expected win rate
            avg_win: Average winning trade PnL %
            avg_loss: Average losing trade PnL %
            
        Returns:
            List of simulated TradeOutcome.
        """
        trades = []
        
        for i in range(num_trades):
            is_win = np.random.random() < win_rate
            pnl_pct = (
                avg_win + np.random.normal(0, 1)
                if is_win
                else avg_loss + np.random.normal(0, 0.5)
            )
            
            entry_price = 100 + np.random.normal(0, 5)
            exit_price = entry_price * (1 + pnl_pct / 100)
            
            trades.append(TradeOutcome(
                entry_price=entry_price,
                exit_price=exit_price,
                direction=np.random.choice(["bullish", "bearish", "neutral"]),
                strategy=np.random.choice([
                    "short_premium", "trend_following", "vertical", "butterfly"
                ]),
                score_at_entry=np.random.uniform(0.2, 0.8),
                confidence_at_entry=np.random.uniform(0.3, 0.9),
                regime_at_entry=np.random.choice([
                    "low_volatility", "high_volatility", "trending", "ranging"
                ]),
                pnl=pnl_pct * entry_price / 100,
                pnl_percent=pnl_pct,
                holding_time_minutes=np.random.randint(5, 240),
                win=is_win,
            ))
        
        return trades
    
    @property
    def best_params(self) -> Dict[str, Any]:
        """Get current best parameters."""
        return self._best_params.copy()
    
    @property
    def best_score(self) -> float:
        """Get current best score."""
        return self._best_score
