"""Unified data connector with fallback support.

Routes data requests through multiple providers with automatic
fallback when primary sources fail.
"""
import asyncio
import os
from datetime import datetime
from typing import Any, Dict, List, Optional, Type

import pandas as pd

from zero_dte_pipeline.config import config
from zero_dte_pipeline.data_connectors.base import (
    DataConnector,
    DataConnectorError,
    TimeoutError,
)
from zero_dte_pipeline.data_connectors.polygon import PolygonConnector
from zero_dte_pipeline.utils.logging import get_logger

logger = get_logger(__name__)


class UnifiedDataConnector:
    """Unified data connector with fallback support.
    
    Routes requests through multiple data providers based on:
    1. Configuration priority
    2. Provider availability
    3. Automatic fallback on failure
    """
    
    # Supported providers
    PROVIDERS: Dict[str, Type[DataConnector]] = {
        "polygon": PolygonConnector,
    }

    # IQFeed is opt-in: do not import/register unless explicitly enabled.
    if (os.getenv("TNT_ENABLE_IQFEED", "0") or "0").strip() == "1":
        try:
            from zero_dte_pipeline.data_connectors.iqfeed import IQFeedConnector

            PROVIDERS["iqfeed"] = IQFeedConnector
        except Exception as exc:
            logger.info("IQFeed not available; provider not registered (%s)", exc)
    
    def __init__(
        self,
        priority: Optional[List[str]] = None,
        timeout: Optional[int] = None,
        lazy_providers: Optional[List[str]] = None,
    ):
        """Initialize the unified connector.
        
        Args:
            priority: List of provider names in priority order
            timeout: Default timeout for operations (defaults to config value)
        """
        self.timeout = timeout or config.default_timeout
        self.priority = priority or config.data_source_priority
        
        # Filter to only supported providers
        self.priority = [p for p in self.priority if p in self.PROVIDERS]
        
        if not self.priority:
            self.priority = ["polygon"]  # Default fallback

        configured_lazy = lazy_providers or config.lazy_data_sources
        self.lazy_providers = {
            p for p in configured_lazy if p in self.priority and p in self.PROVIDERS
        }
        if len(self.lazy_providers) == len(self.priority):
            # Ensure at least one provider connects eagerly
            self.lazy_providers.clear()
        
        # Provider instances
        self._connectors: Dict[str, DataConnector] = {}
        self._connected_providers: List[str] = []
        self._provider_locks: Dict[str, asyncio.Lock] = {}
    
    async def connect(self) -> bool:
        """Connect to data providers in priority order."""
        connected_any = False
        for provider_name in self.priority:
            if provider_name in self.lazy_providers:
                logger.debug(
                    "Deferring connection for %s (lazy provider)",
                    provider_name,
                )
                continue
            connected_any = await self._connect_provider(provider_name) or connected_any
        
        if connected_any:
            return True
        if self.lazy_providers:
            logger.info(
                "All providers deferred; will connect lazily on first data request"
            )
            return True
        return False

    async def _connect_provider(self, provider_name: str) -> bool:
        """Connect to a specific provider if not already connected."""
        if provider_name not in self.PROVIDERS:
            return False
        lock = self._get_provider_lock(provider_name)
        async with lock:
            if provider_name in self._connected_providers:
                return True
            connector_class = self.PROVIDERS.get(provider_name)
            if not connector_class:
                return False
            try:
                connector = connector_class(timeout=self.timeout)
                connected = await connector.connect()
                if connected:
                    self._connectors[provider_name] = connector
                    self._connected_providers.append(provider_name)
                    logger.info(f"Connected to {provider_name}")
                    return True
                logger.warning(
                    "Failed to connect to %s: %s",
                    provider_name,
                    getattr(connector, "last_error", "unknown error"),
                )
            except Exception as exc:
                logger.warning(f"Error connecting to {provider_name}: {exc}")
            return False

    async def _ensure_provider_connected(self, provider_name: str) -> bool:
        if provider_name in self._connected_providers:
            return True
        return await self._connect_provider(provider_name)

    def _get_provider_lock(self, provider_name: str) -> asyncio.Lock:
        lock = self._provider_locks.get(provider_name)
        if lock is None:
            lock = asyncio.Lock()
            self._provider_locks[provider_name] = lock
        return lock
    
    async def disconnect(self) -> None:
        """Disconnect from all providers."""
        for name, connector in self._connectors.items():
            try:
                await connector.disconnect()
            except Exception as e:
                logger.warning(f"Error disconnecting from {name}: {e}")
        
        self._connectors.clear()
        self._connected_providers.clear()

    async def __aenter__(self) -> "UnifiedDataConnector":
        await self.connect()
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self.disconnect()
    
    async def test_all_connections(self) -> Dict[str, Any]:
        """Test connectivity to all configured providers.
        
        Returns:
            Dictionary with results for each provider.
        """
        results: Dict[str, Any] = {
            "timestamp": datetime.now().isoformat(),
            "providers": {},
            "summary": {
                "total": len(self.priority),
                "connected": 0,
                "failed": 0,
            },
        }
        
        for provider_name in self.priority:
            connector_class = self.PROVIDERS.get(provider_name)
            if not connector_class:
                results["providers"][provider_name] = {
                    "error": "Unknown provider",
                    "connected": False,
                }
                continue
            
            try:
                connector = connector_class(timeout=self.timeout)
                test_result = await connector.test_connection()
                results["providers"][provider_name] = test_result
                
                if test_result.get("connected"):
                    results["summary"]["connected"] += 1
                else:
                    results["summary"]["failed"] += 1
                
                # Clean up
                await connector.disconnect()
                
            except Exception as e:
                results["providers"][provider_name] = {
                    "error": str(e),
                    "connected": False,
                }
                results["summary"]["failed"] += 1
        
        return results
    
    async def _execute_with_fallback(
        self,
        method_name: str,
        *args,
        providers: Optional[List[str]] = None,
        **kwargs,
    ) -> Optional[Any]:
        """Execute a method with fallback support.
        
        Tries each connected provider in priority order until one succeeds.
        
        Args:
            method_name: Name of the method to call
            *args: Positional arguments
            **kwargs: Keyword arguments
            
        Returns:
            Result from the first successful provider or None.
        """
        errors = []
        provider_order = providers or self.priority

        # Fail fast if a caller explicitly requests IQFeed while it's disabled.
        if providers and any(str(p).strip().lower() == "iqfeed" for p in providers):
            if (os.getenv("TNT_ENABLE_IQFEED", "0") or "0").strip() != "1":
                raise RuntimeError("IQFeed disabled (set TNT_ENABLE_IQFEED=1 to enable)")
        
        for provider_name in provider_order:
            if not await self._ensure_provider_connected(provider_name):
                continue
            connector = self._connectors.get(provider_name)
            if not connector:
                continue
            
            method = getattr(connector, method_name, None)
            if not method:
                continue
            
            try:
                result = await method(*args, **kwargs)
                if result is not None:
                    logger.debug(f"Got result from {provider_name} for {method_name}")
                    return result
            except TimeoutError as e:
                logger.warning(f"{provider_name} timed out for {method_name}: {e}")
                errors.append((provider_name, str(e)))
            except Exception as e:
                logger.warning(f"{provider_name} error for {method_name}: {e}")
                errors.append((provider_name, str(e)))
        
        if errors:
            logger.error(f"All providers failed for {method_name}: {errors}")
        
        return None
    
    async def get_quote(
        self,
        symbol: str,
        providers: Optional[List[str]] = None,
    ) -> Optional[Dict[str, Any]]:
        """Get quote with automatic fallback."""
        return await self._execute_with_fallback(
            "get_quote",
            symbol,
            providers=providers,
        )
    
    async def get_options_chain(
        self,
        symbol: str,
        expiration: Optional[datetime] = None,
        providers: Optional[List[str]] = None,
    ) -> Optional[pd.DataFrame]:
        """Get options chain with automatic fallback."""
        return await self._execute_with_fallback(
            "get_options_chain",
            symbol,
            expiration,
            providers=providers,
        )
    
    async def get_historical_bars(
        self,
        symbol: str,
        start: datetime,
        end: datetime,
        timeframe: str = "1d",
        providers: Optional[List[str]] = None,
    ) -> Optional[pd.DataFrame]:
        """Get historical bars with automatic fallback."""
        return await self._execute_with_fallback(
            "get_historical_bars",
            symbol,
            start,
            end,
            timeframe,
            providers=providers,
        )
    
    @property
    def connected_providers(self) -> List[str]:
        """Return list of connected providers."""
        return self._connected_providers.copy()
    
    @property
    def primary_provider(self) -> Optional[str]:
        """Return the primary (highest priority) connected provider."""
        return self._connected_providers[0] if self._connected_providers else None
