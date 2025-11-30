"""IQFeed data connector.

Handles IQFeed connectivity with proper authentication,
including fixes for the authentication loop and credential loading.
"""
import asyncio
import os
import socket
import struct
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

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


class IQFeedConnector(DataConnector):
    """IQFeed data connector with improved authentication handling.
    
    Fixes:
    - Authentication loop prevention
    - Proper password detection and handling
    - Registry credential loading fallback
    """
    
    # IQFeed ports
    ADMIN_PORT = 9300
    LEVEL1_PORT = 5009
    LOOKUP_PORT = 9100
    HISTORY_PORT = 9100
    
    def __init__(
        self,
        host: str = "127.0.0.1",
        login: Optional[str] = None,
        password: Optional[str] = None,
        product_id: Optional[str] = None,
        timeout: int = 30,
    ):
        super().__init__(timeout=timeout)
        self.host = host
        
        # Load credentials with fallback hierarchy:
        # 1. Explicitly passed parameters
        # 2. Environment variables / .env file
        # 3. Windows registry (if available)
        self._login = login or config.iqfeed_login or self._load_registry_credential("Login")
        self._password = password or config.iqfeed_password or self._load_registry_credential("Password")
        self._product_id = product_id or config.iqfeed_product_id or self._load_registry_credential("ProductID")
        
        # Connection state
        self._admin_socket: Optional[socket.socket] = None
        self._level1_socket: Optional[socket.socket] = None
        self._lookup_socket: Optional[socket.socket] = None
        
        # Authentication state - prevents auth loop
        self._auth_attempts = 0
        self._max_auth_attempts = 3
        self._authenticated = False
    
    def _load_registry_credential(self, key: str) -> Optional[str]:
        """Load credential from Windows registry (if available).
        
        Args:
            key: Registry key name (Login, Password, ProductID)
            
        Returns:
            Credential value or None if not available.
        """
        try:
            import winreg
            reg_path = r"SOFTWARE\DTN\IQFeed\Startup"
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, reg_path) as reg_key:
                value, _ = winreg.QueryValueEx(reg_key, key)
                return value
        except ImportError:
            # Not on Windows
            return None
        except (FileNotFoundError, OSError):
            # Registry key not found
            return None
    
    async def connect(self) -> bool:
        """Establish connection to IQFeed.
        
        Returns:
            True if connection successful, False otherwise.
        """
        try:
            # Check if IQConnect.exe is running
            if not await self._check_iqconnect_running():
                logger.warning("IQConnect.exe not detected, attempting to start")
                await self._start_iqconnect()
            
            # Connect to admin port first
            self._admin_socket = await self._connect_socket(self.ADMIN_PORT)
            if not self._admin_socket:
                self._last_error = "Failed to connect to IQFeed admin port"
                return False
            
            # Authenticate
            if not await self._authenticate():
                self._last_error = "Authentication failed"
                return False
            
            # Connect to data ports
            self._level1_socket = await self._connect_socket(self.LEVEL1_PORT)
            self._lookup_socket = await self._connect_socket(self.LOOKUP_PORT)
            
            if not self._level1_socket or not self._lookup_socket:
                self._last_error = "Failed to connect to IQFeed data ports"
                return False
            
            self._connected = True
            logger.info("Successfully connected to IQFeed")
            return True
            
        except Exception as e:
            self._last_error = str(e)
            logger.error(f"IQFeed connection error: {e}")
            return False
    
    async def _check_iqconnect_running(self) -> bool:
        """Check if IQConnect.exe is running."""
        try:
            # Try to connect to admin port
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(2)
            result = sock.connect_ex((self.host, self.ADMIN_PORT))
            sock.close()
            return result == 0
        except Exception:
            return False
    
    async def _start_iqconnect(self) -> None:
        """Attempt to start IQConnect.exe."""
        # This is Windows-specific
        try:
            import subprocess
            iqconnect_path = os.environ.get("IQFEED_PATH", r"C:\Program Files\DTN\IQFeed\iqconnect.exe")
            
            args = [
                iqconnect_path,
                f"-product {self._product_id}" if self._product_id else "",
                f"-login {self._login}" if self._login else "",
                f"-password {self._password}" if self._password else "",
                "-autoconnect",
            ]
            args = [a for a in args if a]
            
            subprocess.Popen(args, shell=True)
            
            # Wait for IQConnect to start
            for _ in range(10):
                await asyncio.sleep(1)
                if await self._check_iqconnect_running():
                    return
            
            raise ConnectionError("IQConnect.exe failed to start")
        except Exception as e:
            logger.warning(f"Could not start IQConnect: {e}")
    
    async def _connect_socket(self, port: int) -> Optional[socket.socket]:
        """Connect to an IQFeed port.
        
        Args:
            port: The port to connect to
            
        Returns:
            Connected socket or None on failure.
        """
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(self.timeout)
            sock.connect((self.host, port))
            sock.setblocking(False)
            return sock
        except Exception as e:
            logger.error(f"Failed to connect to port {port}: {e}")
            return None
    
    async def _authenticate(self) -> bool:
        """Authenticate with IQFeed.
        
        Implements fixes for:
        - Authentication loop (max attempts)
        - Incorrect password detection
        
        Returns:
            True if authenticated, False otherwise.
        """
        if self._authenticated:
            return True
        
        if self._auth_attempts >= self._max_auth_attempts:
            logger.error("Max authentication attempts reached, stopping to prevent auth loop")
            return False
        
        self._auth_attempts += 1
        
        try:
            if not self._admin_socket:
                return False
            
            # Send protocol request
            await self._send_command(self._admin_socket, "S,SET PROTOCOL,6.2")
            
            # Read response - check for password error
            response = await self._read_response(self._admin_socket)
            
            if "incorrect password" in response.lower():
                logger.error("IQFeed: Incorrect password detected")
                self._last_error = "Incorrect password"
                return False
            
            if "not authorized" in response.lower():
                logger.error("IQFeed: Not authorized")
                self._last_error = "Not authorized - check credentials"
                return False
            
            if "S,CURRENT PROTOCOL" in response:
                self._authenticated = True
                return True
            
            return False
            
        except Exception as e:
            logger.error(f"Authentication error: {e}")
            return False
    
    async def _send_command(self, sock: socket.socket, command: str) -> None:
        """Send a command to IQFeed."""
        loop = asyncio.get_event_loop()
        await loop.sock_sendall(sock, f"{command}\r\n".encode())
    
    async def _read_response(self, sock: socket.socket, timeout: Optional[int] = None) -> str:
        """Read response from IQFeed."""
        timeout = timeout or self.timeout
        loop = asyncio.get_event_loop()
        
        try:
            data = await asyncio.wait_for(
                loop.sock_recv(sock, 4096),
                timeout=timeout
            )
            return data.decode("utf-8", errors="ignore")
        except asyncio.TimeoutError:
            raise TimeoutError("Read timeout")
    
    async def disconnect(self) -> None:
        """Disconnect from IQFeed."""
        for sock in [self._admin_socket, self._level1_socket, self._lookup_socket]:
            if sock:
                try:
                    sock.close()
                except Exception:
                    pass
        
        self._admin_socket = None
        self._level1_socket = None
        self._lookup_socket = None
        self._connected = False
        self._authenticated = False
        logger.info("Disconnected from IQFeed")
    
    async def test_connection(self) -> Dict[str, Any]:
        """Test IQFeed connection."""
        result: Dict[str, Any] = {
            "provider": "iqfeed",
            "connected": False,
            "authenticated": False,
        }
        
        start_time = datetime.now()
        
        try:
            # Check credentials
            has_credentials = bool(self._login and self._password)
            result["has_credentials"] = has_credentials
            
            if not has_credentials:
                result["error"] = "Missing IQFeed credentials"
                return result
            
            # Test connection
            connected = await self.connect()
            result["connected"] = connected
            result["authenticated"] = self._authenticated
            
            if connected:
                # Measure latency with a simple request
                latency_start = datetime.now()
                quote = await self.get_quote("SPY")
                if quote:
                    result["latency_ms"] = (datetime.now() - latency_start).total_seconds() * 1000
                    result["test_quote"] = quote
            else:
                result["error"] = self._last_error
                
        except Exception as e:
            result["error"] = str(e)
        
        result["total_time_ms"] = (datetime.now() - start_time).total_seconds() * 1000
        return result
    
    async def get_quote(self, symbol: str) -> Optional[Dict[str, Any]]:
        """Get current quote for a symbol."""
        if not self._connected or not self._level1_socket:
            return None
        
        try:
            # Request fundamental data
            await self._send_command(self._level1_socket, f"rSY,{symbol}")
            response = await self._read_response(self._level1_socket)
            
            # Parse response
            fields = response.strip().split(",")
            if len(fields) < 10:
                return None
            
            return {
                "symbol": symbol,
                "bid": float(fields[1]) if fields[1] else None,
                "ask": float(fields[2]) if fields[2] else None,
                "last": float(fields[3]) if fields[3] else None,
                "volume": int(fields[4]) if fields[4] else None,
                "timestamp": datetime.now().isoformat(),
            }
        except Exception as e:
            logger.error(f"Error getting quote for {symbol}: {e}")
            return None
    
    async def get_options_chain(
        self,
        symbol: str,
        expiration: Optional[datetime] = None,
    ) -> Optional[pd.DataFrame]:
        """Get options chain for a symbol.
        
        If expiration is None, returns 0DTE options (today's expiration).
        """
        if not self._connected or not self._lookup_socket:
            return None
        
        try:
            # Default to 0DTE
            if expiration is None:
                expiration = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
            
            exp_str = expiration.strftime("%Y%m%d")
            
            # Request options chain
            await self._send_command(
                self._lookup_socket,
                f"COE,{symbol},{exp_str},,0,0,0"
            )
            
            # Read and parse response
            options = []
            while True:
                response = await self._read_response(self._lookup_socket, timeout=5)
                if "!ENDMSG!" in response:
                    break
                
                for line in response.strip().split("\n"):
                    if line.startswith("E,"):
                        # Skip error messages
                        continue
                    fields = line.split(",")
                    if len(fields) >= 10:
                        try:
                            options.append({
                                "symbol": fields[0],
                                "strike": float(fields[1]),
                                "type": "call" if "C" in fields[0] else "put",
                                "expiration": expiration,
                                "bid": float(fields[2]) if fields[2] else None,
                                "ask": float(fields[3]) if fields[3] else None,
                                "last": float(fields[4]) if fields[4] else None,
                                "volume": int(fields[5]) if fields[5] else 0,
                                "open_interest": int(fields[6]) if fields[6] else 0,
                                "iv": float(fields[7]) if fields[7] else None,
                                "delta": float(fields[8]) if fields[8] else None,
                                "gamma": float(fields[9]) if fields[9] else None,
                            })
                        except (ValueError, IndexError):
                            continue
            
            if not options:
                return None
            
            return pd.DataFrame(options)
            
        except Exception as e:
            logger.error(f"Error getting options chain for {symbol}: {e}")
            return None
    
    async def get_historical_bars(
        self,
        symbol: str,
        start: datetime,
        end: datetime,
        timeframe: str = "1d",
    ) -> Optional[pd.DataFrame]:
        """Get historical price bars."""
        if not self._connected or not self._lookup_socket:
            return None
        
        try:
            # Convert timeframe to IQFeed format
            tf_map = {
                "1m": ("HIT", 60),
                "5m": ("HIT", 300),
                "15m": ("HIT", 900),
                "1h": ("HIT", 3600),
                "1d": ("HDT", 86400),
            }
            
            cmd_prefix, interval = tf_map.get(timeframe, ("HDT", 86400))
            
            start_str = start.strftime("%Y%m%d")
            end_str = end.strftime("%Y%m%d")
            
            # Request historical data
            await self._send_command(
                self._lookup_socket,
                f"{cmd_prefix},{symbol},{start_str},{end_str},"
            )
            
            # Read and parse response
            bars = []
            while True:
                response = await self._read_response(self._lookup_socket, timeout=5)
                if "!ENDMSG!" in response:
                    break
                
                for line in response.strip().split("\n"):
                    if line.startswith("E,"):
                        continue
                    fields = line.split(",")
                    if len(fields) >= 7:
                        try:
                            bars.append({
                                "timestamp": fields[0],
                                "open": float(fields[1]),
                                "high": float(fields[2]),
                                "low": float(fields[3]),
                                "close": float(fields[4]),
                                "volume": int(fields[5]),
                            })
                        except (ValueError, IndexError):
                            continue
            
            if not bars:
                return None
            
            df = pd.DataFrame(bars)
            df["timestamp"] = pd.to_datetime(df["timestamp"])
            df.set_index("timestamp", inplace=True)
            return df
            
        except Exception as e:
            logger.error(f"Error getting historical bars for {symbol}: {e}")
            return None
