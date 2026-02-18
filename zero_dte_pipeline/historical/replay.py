"""Historical data replay for backtesting.

Replays captured historical data for strategy testing.
"""
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Dict, Generator, List, Optional

import pandas as pd

from zero_dte_pipeline.candidates.scoring import Candidate, SignalAlignment
from zero_dte_pipeline.historical.capture import HistoricalCapture
from zero_dte_pipeline.utils.logging import get_logger

logger = get_logger(__name__)


@dataclass
class ReplayTick:
    """A single replay tick with market data."""
    timestamp: datetime
    underlying: str
    chain_data: pd.DataFrame
    bar_data: Optional[pd.DataFrame] = None
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class BacktestResult:
    """Results from a backtest run."""
    start_date: datetime
    end_date: datetime
    total_trades: int
    winning_trades: int
    losing_trades: int
    total_pnl: float
    win_rate: float
    avg_win: float
    avg_loss: float
    max_drawdown: float
    sharpe_ratio: float
    trades: List[Dict[str, Any]]
    equity_curve: List[float]
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary."""
        return {
            "start_date": self.start_date.isoformat(),
            "end_date": self.end_date.isoformat(),
            "total_trades": self.total_trades,
            "winning_trades": self.winning_trades,
            "losing_trades": self.losing_trades,
            "total_pnl": self.total_pnl,
            "win_rate": self.win_rate,
            "avg_win": self.avg_win,
            "avg_loss": self.avg_loss,
            "max_drawdown": self.max_drawdown,
            "sharpe_ratio": self.sharpe_ratio,
            "trade_count": len(self.trades),
        }


class HistoricalReplay:
    """Replays historical data for backtesting.
    
    Features:
    - Sequential tick replay
    - Strategy callback support
    - Performance metrics calculation
    """
    
    def __init__(
        self,
        storage_path: Path = Path("data/historical"),
    ):
        self.storage_path = storage_path
        self._captures: List[Dict[str, Any]] = []
        self._load_captures()
    
    def _load_captures(self) -> None:
        """Load capture metadata."""
        metadata_path = self.storage_path / "metadata.json"
        if metadata_path.exists():
            import json
            with open(metadata_path, "r") as f:
                data = json.load(f)
                self._captures = data.get("captures", [])
    
    def get_available_dates(
        self,
        underlying: Optional[str] = None,
    ) -> List[datetime]:
        """Get list of available dates with captured data.
        
        Args:
            underlying: Filter by underlying symbol
            
        Returns:
            List of dates with available data.
        """
        dates = set()
        
        for capture in self._captures:
            if underlying and capture["underlying"] != underlying:
                continue
            
            exp_date = datetime.fromisoformat(capture["expiration"]).date()
            dates.add(datetime.combine(exp_date, datetime.min.time()))
        
        return sorted(dates)
    
    def load_data_for_date(
        self,
        date: datetime,
        underlying: Optional[str] = None,
    ) -> Dict[str, pd.DataFrame]:
        """Load all captured data for a specific date.
        
        Args:
            date: The date to load data for
            underlying: Optional filter by underlying
            
        Returns:
            Dictionary mapping underlying to DataFrame.
        """
        data: Dict[str, pd.DataFrame] = {}
        date_str = date.strftime("%Y-%m-%d")
        
        for capture in self._captures:
            if underlying and capture["underlying"] != underlying:
                continue
            
            exp_date = datetime.fromisoformat(capture["expiration"]).strftime("%Y-%m-%d")
            if exp_date != date_str:
                continue
            
            filepath = Path(capture["filepath"])
            if not filepath.exists():
                continue
            
            try:
                if filepath.suffix == ".parquet":
                    df = pd.read_parquet(filepath)
                else:
                    df = pd.read_csv(filepath)
                
                ul = capture["underlying"]
                if ul in data:
                    data[ul] = pd.concat([data[ul], df], ignore_index=True)
                else:
                    data[ul] = df
                    
            except Exception as e:
                logger.error(f"Error loading {filepath}: {e}")
        
        return data
    
    def replay_ticks(
        self,
        start_date: datetime,
        end_date: datetime,
        underlying: Optional[str] = None,
    ) -> Generator[ReplayTick, None, None]:
        """Generate replay ticks for a date range.
        
        Args:
            start_date: Start of replay period
            end_date: End of replay period
            underlying: Optional filter by underlying
            
        Yields:
            ReplayTick objects in chronological order.
        """
        current = start_date
        
        while current <= end_date:
            data = self.load_data_for_date(current, underlying)
            
            for ul, chain_df in data.items():
                yield ReplayTick(
                    timestamp=current,
                    underlying=ul,
                    chain_data=chain_df,
                    metadata={
                        "date": current.strftime("%Y-%m-%d"),
                        "underlying": ul,
                        "options_count": len(chain_df),
                    },
                )
            
            current += timedelta(days=1)
    
    def run_backtest(
        self,
        start_date: datetime,
        end_date: datetime,
        strategy_callback: Callable[[ReplayTick, List[Dict]], List[Dict]],
        initial_capital: float = 100000.0,
        underlying: Optional[str] = None,
    ) -> BacktestResult:
        """Run a backtest over historical data.
        
        Args:
            start_date: Start of backtest period
            end_date: End of backtest period
            strategy_callback: Function that receives tick and open positions,
                              returns list of trade actions
            initial_capital: Starting capital
            underlying: Optional filter by underlying
            
        Returns:
            BacktestResult with performance metrics.
        """
        trades: List[Dict[str, Any]] = []
        open_positions: List[Dict[str, Any]] = []
        equity_curve: List[float] = [initial_capital]
        current_capital = initial_capital
        
        logger.info(f"Starting backtest from {start_date.date()} to {end_date.date()}")
        
        for tick in self.replay_ticks(start_date, end_date, underlying):
            # Call strategy
            try:
                actions = strategy_callback(tick, open_positions)
            except Exception as e:
                logger.error(f"Strategy error on {tick.timestamp}: {e}")
                actions = []
            
            # Process actions
            for action in actions:
                if action.get("action") == "open":
                    open_positions.append({
                        "entry_time": tick.timestamp,
                        "entry_price": action.get("price", 0),
                        "symbol": action.get("symbol"),
                        "direction": action.get("direction"),
                        "size": action.get("size", 1),
                    })
                    
                elif action.get("action") == "close":
                    # Find and close position
                    symbol = action.get("symbol")
                    for pos in open_positions:
                        if pos["symbol"] == symbol:
                            pnl = (action.get("price", 0) - pos["entry_price"]) * pos["size"]
                            if pos["direction"] == "bearish":
                                pnl = -pnl
                            
                            trades.append({
                                "entry_time": pos["entry_time"],
                                "exit_time": tick.timestamp,
                                "symbol": symbol,
                                "entry_price": pos["entry_price"],
                                "exit_price": action.get("price", 0),
                                "pnl": pnl,
                                "direction": pos["direction"],
                            })
                            
                            current_capital += pnl
                            open_positions.remove(pos)
                            break
            
            equity_curve.append(current_capital)
        
        # Calculate metrics
        winning_trades = [t for t in trades if t["pnl"] > 0]
        losing_trades = [t for t in trades if t["pnl"] <= 0]
        
        total_pnl = sum(t["pnl"] for t in trades)
        win_rate = len(winning_trades) / len(trades) if trades else 0
        avg_win = sum(t["pnl"] for t in winning_trades) / len(winning_trades) if winning_trades else 0
        avg_loss = sum(t["pnl"] for t in losing_trades) / len(losing_trades) if losing_trades else 0
        
        # Calculate drawdown
        peak = initial_capital
        max_drawdown = 0
        for equity in equity_curve:
            if equity > peak:
                peak = equity
            drawdown = (peak - equity) / peak
            if drawdown > max_drawdown:
                max_drawdown = drawdown
        
        # Calculate Sharpe ratio (simplified)
        import numpy as np
        returns = np.diff(equity_curve) / np.array(equity_curve[:-1])
        sharpe = np.mean(returns) / np.std(returns) * np.sqrt(252) if len(returns) > 1 and np.std(returns) > 0 else 0
        
        return BacktestResult(
            start_date=start_date,
            end_date=end_date,
            total_trades=len(trades),
            winning_trades=len(winning_trades),
            losing_trades=len(losing_trades),
            total_pnl=total_pnl,
            win_rate=win_rate,
            avg_win=avg_win,
            avg_loss=avg_loss,
            max_drawdown=max_drawdown,
            sharpe_ratio=float(sharpe),
            trades=trades,
            equity_curve=equity_curve,
        )
    
    def generate_sample_data(
        self,
        days: int = 30,
        underlyings: Optional[List[str]] = None,
    ) -> None:
        """Generate sample historical data for testing.
        
        Args:
            days: Number of days of data to generate
            underlyings: List of underlyings to generate data for
        """
        import numpy as np
        
        underlyings = underlyings or ["SPY", "QQQ", "IWM"]
        storage = self.storage_path
        storage.mkdir(parents=True, exist_ok=True)
        
        metadata = {"captures": [], "last_updated": datetime.now().isoformat()}
        
        end_date = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
        start_date = end_date - timedelta(days=days)
        
        current = start_date
        while current <= end_date:
            # Skip weekends
            if current.weekday() >= 5:
                current += timedelta(days=1)
                continue
            
            for ul in underlyings:
                # Generate synthetic chain data
                base_price = {"SPY": 450, "QQQ": 380, "IWM": 200}.get(ul, 100)
                price = base_price + np.random.normal(0, base_price * 0.02)
                
                strikes = np.arange(price * 0.9, price * 1.1, 1)
                options = []
                
                for strike in strikes:
                    for opt_type in ["call", "put"]:
                        delta = (price - strike) / price if opt_type == "call" else (strike - price) / price
                        delta = np.clip(delta + np.random.normal(0, 0.1), -1, 1)
                        
                        iv = 0.2 + np.random.uniform(-0.05, 0.05)
                        
                        options.append({
                            "symbol": f"{ul}{current.strftime('%y%m%d')}{'C' if opt_type == 'call' else 'P'}{int(strike * 1000)}",
                            "strike": strike,
                            "type": opt_type,
                            "expiration": current,
                            "bid": max(0.01, np.random.uniform(0.5, 5)),
                            "ask": max(0.02, np.random.uniform(0.6, 5.5)),
                            "last": max(0.01, np.random.uniform(0.55, 5.25)),
                            "volume": np.random.randint(10, 5000),
                            "open_interest": np.random.randint(100, 50000),
                            "iv": iv,
                            "delta": delta,
                            "gamma": np.random.uniform(0.01, 0.1),
                            "theta": -np.random.uniform(0.01, 0.2),
                            "vega": np.random.uniform(0.05, 0.3),
                        })
                
                df = pd.DataFrame(options)
                
                # Save to file
                date_str = current.strftime("%Y%m%d")
                filename = f"{ul}_{date_str}_sample.parquet"
                filepath = storage / filename
                df.to_parquet(filepath, index=False)
                
                metadata["captures"].append({
                    "underlying": ul,
                    "expiration": current.isoformat(),
                    "capture_time": datetime.now().isoformat(),
                    "filepath": str(filepath),
                    "rows": len(df),
                    "format": "parquet",
                })
            
            current += timedelta(days=1)
        
        # Save metadata
        import json
        with open(storage / "metadata.json", "w") as f:
            json.dump(metadata, f, indent=2)
        
        logger.info(f"Generated sample data for {days} days")
