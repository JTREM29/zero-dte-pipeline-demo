"""Base class for data connectors."""
import asyncio
from abc import ABC, abstractmethod
from datetime import datetime
from typing import Any, Dict, List, Optional

import pandas as pd


class DataConnectorError(Exception):
    """Base exception for data connector errors."""
    pass


class AuthenticationError(DataConnectorError):
    """Authentication failed."""
    pass


class ConnectionError(DataConnectorError):
    """Connection to data source failed."""
    pass


class TimeoutError(DataConnectorError):
    """Operation timed out."""
    pass


class DataConnector(ABC):
    """Abstract base class for data connectors."""
    
    def __init__(self, timeout: int = 30):
        self.timeout = timeout
        self._connected = False
        self._last_error: Optional[str] = None
    
    @property
    def is_connected(self) -> bool:
        """Return whether connector is currently connected."""
        return self._connected
    
    @property
    def last_error(self) -> Optional[str]:
        """Return the last error message."""
        return self._last_error
    
    @abstractmethod
    async def connect(self) -> bool:
        """Establish connection to the data source.
        
        Returns:
            True if connection successful, False otherwise.
        """
        pass
    
    @abstractmethod
    async def disconnect(self) -> None:
        """Disconnect from the data source."""
        pass
    
    @abstractmethod
    async def test_connection(self) -> Dict[str, Any]:
        """Test the connection and return status info.
        
        Returns:
            Dictionary with keys:
            - 'connected': bool
            - 'latency_ms': float (optional)
            - 'error': str (optional)
            - 'details': dict (optional)
        """
        pass
    
    @abstractmethod
    async def get_quote(self, symbol: str) -> Optional[Dict[str, Any]]:
        """Get current quote for a symbol.
        
        Args:
            symbol: The ticker symbol
            
        Returns:
            Quote data dictionary or None if not available.
        """
        pass
    
    @abstractmethod
    async def get_options_chain(
        self,
        symbol: str,
        expiration: Optional[datetime] = None,
    ) -> Optional[pd.DataFrame]:
        """Get options chain for a symbol.
        
        Args:
            symbol: The underlying ticker symbol
            expiration: Optional specific expiration date (defaults to 0DTE)
            
        Returns:
            DataFrame with options chain data or None if not available.
        """
        pass
    
    @abstractmethod
    async def get_historical_bars(
        self,
        symbol: str,
        start: datetime,
        end: datetime,
        timeframe: str = "1d",
    ) -> Optional[pd.DataFrame]:
        """Get historical price bars.
        
        Args:
            symbol: The ticker symbol
            start: Start datetime
            end: End datetime
            timeframe: Bar timeframe (e.g., '1m', '5m', '1h', '1d')
            
        Returns:
            DataFrame with OHLCV data or None if not available.
        """
        pass
    
    async def _with_timeout(self, coro, timeout: Optional[int] = None):
        """Wrap a coroutine with a timeout.
        
        Args:
            coro: The coroutine to wrap
            timeout: Timeout in seconds (defaults to self.timeout)
            
        Returns:
            The result of the coroutine
            
        Raises:
            TimeoutError: If the operation times out
        """
        timeout = timeout or self.timeout
        try:
            return await asyncio.wait_for(coro, timeout=timeout)
        except asyncio.TimeoutError:
            raise TimeoutError(f"Operation timed out after {timeout} seconds")
