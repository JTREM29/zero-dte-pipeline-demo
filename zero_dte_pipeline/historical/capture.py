"""Historical data capture module.

Downloads and stores options chain data for backtesting.
"""
import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

import pandas as pd

from zero_dte_pipeline.data_connectors.unified import UnifiedDataConnector
from zero_dte_pipeline.utils.logging import get_logger
from zero_dte_pipeline.utils.timeout import with_timeout

logger = get_logger(__name__)


class HistoricalCapture:
    """Captures and stores historical options data.
    
    Features:
    - Automatic chain downloads
    - Multiple storage formats (CSV, Parquet)
    - Metadata tracking
    """
    
    DEFAULT_UNDERLYINGS = ["SPX", "SPY", "QQQ", "IWM"]
    
    def __init__(
        self,
        data_connector: UnifiedDataConnector,
        storage_path: Path = Path("data/historical"),
    ):
        self.data_connector = data_connector
        self.storage_path = storage_path
        self.storage_path.mkdir(parents=True, exist_ok=True)
        
        # Metadata file
        self.metadata_path = self.storage_path / "metadata.json"
        self._metadata = self._load_metadata()
    
    def _load_metadata(self) -> Dict[str, Any]:
        """Load metadata from file."""
        if self.metadata_path.exists():
            with open(self.metadata_path, "r") as f:
                return json.load(f)
        return {"captures": [], "last_updated": None}
    
    def _save_metadata(self) -> None:
        """Save metadata to file."""
        self._metadata["last_updated"] = datetime.now().isoformat()
        with open(self.metadata_path, "w") as f:
            json.dump(self._metadata, f, indent=2)
    
    async def capture_chain(
        self,
        underlying: str,
        expiration: Optional[datetime] = None,
        save_format: str = "parquet",
    ) -> Optional[Path]:
        """Capture and store an options chain.
        
        Args:
            underlying: The underlying symbol
            expiration: Expiration date (defaults to 0DTE)
            save_format: Storage format ('csv' or 'parquet')
            
        Returns:
            Path to saved file or None on failure.
        """
        if expiration is None:
            expiration = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
        
        logger.info(f"Capturing chain for {underlying} expiring {expiration.date()}")
        
        chain = await with_timeout(
            self.data_connector.get_options_chain(underlying, expiration),
            timeout=60,
            default=None,
            operation_name=f"capture_chain_{underlying}",
        )
        
        if chain is None or chain.empty:
            logger.warning(f"No chain data for {underlying}")
            return None
        
        # Add metadata columns
        chain["capture_timestamp"] = datetime.now()
        chain["underlying"] = underlying
        chain["expiration_date"] = expiration
        
        # Generate filename
        date_str = expiration.strftime("%Y%m%d")
        capture_time = datetime.now().strftime("%H%M%S")
        filename = f"{underlying}_{date_str}_{capture_time}.{save_format}"
        filepath = self.storage_path / filename
        
        # Save data
        if save_format == "parquet":
            chain.to_parquet(filepath, index=False)
        else:
            chain.to_csv(filepath, index=False)
        
        # Update metadata
        capture_info = {
            "underlying": underlying,
            "expiration": expiration.isoformat(),
            "capture_time": datetime.now().isoformat(),
            "filepath": str(filepath),
            "rows": len(chain),
            "format": save_format,
        }
        self._metadata["captures"].append(capture_info)
        self._save_metadata()
        
        logger.info(f"Saved {len(chain)} options to {filepath}")
        return filepath
    
    async def capture_all_chains(
        self,
        underlyings: Optional[List[str]] = None,
        include_0dte_plus_1: bool = True,
        save_format: str = "parquet",
    ) -> Dict[str, List[Path]]:
        """Capture chains for all specified underlyings.
        
        Args:
            underlyings: List of underlyings (defaults to all supported)
            include_0dte_plus_1: Whether to capture 0DTE+1 as well
            save_format: Storage format
            
        Returns:
            Dictionary mapping underlying to list of saved file paths.
        """
        underlyings = underlyings or self.DEFAULT_UNDERLYINGS
        results: Dict[str, List[Path]] = {}
        
        today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
        expirations = [today]
        
        if include_0dte_plus_1:
            expirations.append(today + timedelta(days=1))
        
        for underlying in underlyings:
            results[underlying] = []
            
            for expiration in expirations:
                filepath = await self.capture_chain(
                    underlying, expiration, save_format
                )
                if filepath:
                    results[underlying].append(filepath)
        
        return results
    
    async def capture_historical_bars(
        self,
        symbol: str,
        start: datetime,
        end: datetime,
        timeframe: str = "1d",
        save_format: str = "parquet",
    ) -> Optional[Path]:
        """Capture and store historical price bars.
        
        Args:
            symbol: The symbol to capture
            start: Start date
            end: End date
            timeframe: Bar timeframe
            save_format: Storage format
            
        Returns:
            Path to saved file or None on failure.
        """
        logger.info(f"Capturing bars for {symbol} from {start.date()} to {end.date()}")
        
        bars = await with_timeout(
            self.data_connector.get_historical_bars(symbol, start, end, timeframe),
            timeout=60,
            default=None,
            operation_name=f"capture_bars_{symbol}",
        )
        
        if bars is None or bars.empty:
            logger.warning(f"No bar data for {symbol}")
            return None
        
        # Generate filename
        start_str = start.strftime("%Y%m%d")
        end_str = end.strftime("%Y%m%d")
        filename = f"{symbol}_bars_{timeframe}_{start_str}_{end_str}.{save_format}"
        filepath = self.storage_path / filename
        
        # Save data
        if save_format == "parquet":
            bars.to_parquet(filepath)
        else:
            bars.to_csv(filepath)
        
        logger.info(f"Saved {len(bars)} bars to {filepath}")
        return filepath
    
    def list_captures(
        self,
        underlying: Optional[str] = None,
        start_date: Optional[datetime] = None,
        end_date: Optional[datetime] = None,
    ) -> List[Dict[str, Any]]:
        """List captured data files.
        
        Args:
            underlying: Filter by underlying
            start_date: Filter by start date
            end_date: Filter by end date
            
        Returns:
            List of capture metadata dictionaries.
        """
        captures = self._metadata.get("captures", [])
        
        if underlying:
            captures = [c for c in captures if c["underlying"] == underlying]
        
        if start_date:
            captures = [
                c for c in captures
                if datetime.fromisoformat(c["capture_time"]) >= start_date
            ]
        
        if end_date:
            captures = [
                c for c in captures
                if datetime.fromisoformat(c["capture_time"]) <= end_date
            ]
        
        return captures
    
    def load_capture(self, filepath: str) -> Optional[pd.DataFrame]:
        """Load a captured data file.
        
        Args:
            filepath: Path to the capture file
            
        Returns:
            DataFrame with captured data or None.
        """
        path = Path(filepath)
        if not path.exists():
            logger.error(f"Capture file not found: {filepath}")
            return None
        
        try:
            if path.suffix == ".parquet":
                return pd.read_parquet(path)
            else:
                return pd.read_csv(path)
        except Exception as e:
            logger.error(f"Error loading capture: {e}")
            return None
