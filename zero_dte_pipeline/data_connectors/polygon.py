"""Polygon.io data connector.

Provides market data from Polygon.io REST API as a fallback
when IQFeed is unavailable.
"""
import asyncio
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

import aiohttp
import pandas as pd

from zero_dte_pipeline.config import config
from zero_dte_pipeline.data_connectors.base import (
    AuthenticationError,
    ConnectionError,
    DataConnector,
    DataConnectorError,
    TimeoutError,
)
from zero_dte_pipeline.utils.logging import get_logger

logger = get_logger(__name__)


class PolygonConnector(DataConnector):
    """Polygon.io data connector."""
    
    BASE_URL = "https://api.polygon.io"
    
    def __init__(
        self,
        api_key: Optional[str] = None,
        timeout: int = 30,
    ):
        super().__init__(timeout=timeout)
        self._api_key = api_key or config.polygon_api_key
        self._session: Optional[aiohttp.ClientSession] = None
    
    async def connect(self) -> bool:
        """Establish connection (create HTTP session)."""
        if not self._api_key:
            self._last_error = "No Polygon API key provided"
            logger.error(self._last_error)
            return False
        
        try:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=self.timeout)
            )
            self._connected = True
            logger.info("Polygon connector initialized")
            return True
        except Exception as e:
            self._last_error = str(e)
            logger.error(f"Failed to initialize Polygon connector: {e}")
            return False
    
    async def disconnect(self) -> None:
        """Close the HTTP session."""
        if self._session:
            await self._session.close()
            self._session = None
        self._connected = False
        logger.info("Polygon connector disconnected")
    
    async def _request(self, endpoint: str, params: Optional[Dict] = None) -> Optional[Dict]:
        """Make an authenticated request to Polygon API.
        
        Args:
            endpoint: API endpoint path
            params: Query parameters
            
        Returns:
            JSON response as dictionary or None on failure.
        """
        if not self._session:
            await self.connect()
        
        if not self._session:
            return None
        
        params = params or {}
        params["apiKey"] = self._api_key
        
        url = f"{self.BASE_URL}{endpoint}"
        
        try:
            async with self._session.get(url, params=params) as response:
                if response.status == 401:
                    self._last_error = "Invalid Polygon API key"
                    raise AuthenticationError(self._last_error)
                
                if response.status == 429:
                    self._last_error = "Polygon API rate limit exceeded"
                    raise DataConnectorError(self._last_error)
                
                if response.status != 200:
                    self._last_error = f"Polygon API error: {response.status}"
                    return None
                
                data = await response.json()
                return data
                
        except aiohttp.ClientError as e:
            self._last_error = f"HTTP error: {e}"
            logger.error(self._last_error)
            return None
        except asyncio.TimeoutError:
            self._last_error = "Request timed out"
            raise TimeoutError(self._last_error)
    
    async def test_connection(self) -> Dict[str, Any]:
        """Test Polygon connection."""
        result: Dict[str, Any] = {
            "provider": "polygon",
            "connected": False,
            "has_credentials": bool(self._api_key),
        }
        
        if not self._api_key:
            result["error"] = "No API key configured"
            return result
        
        start_time = datetime.now()
        
        try:
            # Test with a simple ticker lookup
            data = await self._request("/v3/reference/tickers/SPY")
            
            if data and data.get("status") == "OK":
                result["connected"] = True
                result["latency_ms"] = (datetime.now() - start_time).total_seconds() * 1000
            else:
                result["error"] = data.get("error") if data else "Unknown error"
                
        except Exception as e:
            result["error"] = str(e)
        
        result["total_time_ms"] = (datetime.now() - start_time).total_seconds() * 1000
        return result
    
    async def get_quote(self, symbol: str) -> Optional[Dict[str, Any]]:
        """Get current quote for a symbol."""
        data = await self._request(f"/v2/last/trade/{symbol}")
        
        if not data or data.get("status") != "OK":
            return None
        
        result = data.get("results", {})
        
        return {
            "symbol": symbol,
            "last": result.get("p"),
            "size": result.get("s"),
            "timestamp": datetime.fromtimestamp(
                result.get("t", 0) / 1000000000
            ).isoformat() if result.get("t") else None,
        }
    
    async def get_snapshot(self, symbol: str) -> Optional[Dict[str, Any]]:
        """Get full market snapshot for a symbol."""
        data = await self._request(f"/v2/snapshot/locale/us/markets/stocks/tickers/{symbol}")
        
        if not data or data.get("status") != "OK":
            return None
        
        ticker = data.get("ticker", {})
        
        return {
            "symbol": symbol,
            "bid": ticker.get("lastQuote", {}).get("P"),
            "ask": ticker.get("lastQuote", {}).get("p"),
            "last": ticker.get("lastTrade", {}).get("p"),
            "volume": ticker.get("day", {}).get("v"),
            "open": ticker.get("day", {}).get("o"),
            "high": ticker.get("day", {}).get("h"),
            "low": ticker.get("day", {}).get("l"),
            "close": ticker.get("prevDay", {}).get("c"),
            "change": ticker.get("todaysChange"),
            "change_percent": ticker.get("todaysChangePerc"),
        }
    
    async def get_options_chain(
        self,
        symbol: str,
        expiration: Optional[datetime] = None,
    ) -> Optional[pd.DataFrame]:
        """Get options chain for a symbol."""
        # Default to 0DTE
        if expiration is None:
            expiration = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
        
        exp_str = expiration.strftime("%Y-%m-%d")
        
        # Get options contracts
        params = {
            "underlying_ticker": symbol,
            "expiration_date": exp_str,
            "limit": 1000,
        }
        
        data = await self._request("/v3/reference/options/contracts", params)
        
        if not data or data.get("status") != "OK":
            return None
        
        contracts = data.get("results", [])
        
        if not contracts:
            return None
        
        # Get snapshots for each contract
        options = []
        for contract in contracts:
            ticker = contract.get("ticker", "")
            
            # Get snapshot for this option
            snapshot_data = await self._request(
                f"/v3/snapshot/options/{symbol}/{ticker}"
            )
            
            if snapshot_data and snapshot_data.get("status") == "OK":
                snapshot = snapshot_data.get("results", {})
                greeks = snapshot.get("greeks", {})
                underlying = snapshot.get("underlying_asset", {})
                
                options.append({
                    "symbol": ticker,
                    "strike": contract.get("strike_price"),
                    "type": contract.get("contract_type", "").lower(),
                    "expiration": expiration,
                    "bid": snapshot.get("day", {}).get("bid"),
                    "ask": snapshot.get("day", {}).get("ask"),
                    "last": snapshot.get("day", {}).get("close"),
                    "volume": snapshot.get("day", {}).get("volume"),
                    "open_interest": snapshot.get("open_interest"),
                    "iv": snapshot.get("implied_volatility"),
                    "delta": greeks.get("delta"),
                    "gamma": greeks.get("gamma"),
                    "theta": greeks.get("theta"),
                    "vega": greeks.get("vega"),
                    "underlying_price": underlying.get("price"),
                })
        
        if not options:
            return None
        
        return pd.DataFrame(options)
    
    async def get_historical_bars(
        self,
        symbol: str,
        start: datetime,
        end: datetime,
        timeframe: str = "1d",
    ) -> Optional[pd.DataFrame]:
        """Get historical price bars."""
        # Parse timeframe
        tf_map = {
            "1m": ("minute", 1),
            "5m": ("minute", 5),
            "15m": ("minute", 15),
            "1h": ("hour", 1),
            "1d": ("day", 1),
        }
        
        multiplier_str, multiplier = tf_map.get(timeframe, ("day", 1))
        
        start_str = start.strftime("%Y-%m-%d")
        end_str = end.strftime("%Y-%m-%d")
        
        endpoint = f"/v2/aggs/ticker/{symbol}/range/{multiplier}/{multiplier_str}/{start_str}/{end_str}"
        
        params = {
            "adjusted": "true",
            "sort": "asc",
            "limit": 50000,
        }
        
        data = await self._request(endpoint, params)
        
        if not data or data.get("status") != "OK":
            return None
        
        results = data.get("results", [])
        
        if not results:
            return None
        
        bars = []
        for r in results:
            bars.append({
                "timestamp": datetime.fromtimestamp(r["t"] / 1000),
                "open": r["o"],
                "high": r["h"],
                "low": r["l"],
                "close": r["c"],
                "volume": r["v"],
                "vwap": r.get("vw"),
            })
        
        df = pd.DataFrame(bars)
        df.set_index("timestamp", inplace=True)
        return df
