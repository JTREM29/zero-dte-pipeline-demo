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
    
    # IQFeed ports (defaults can be overridden via config/environment)
    ADMIN_PORT = 9300
    LEVEL1_PORT = 5009
    LOOKUP_PORT = 9100
    HISTORY_PORT = 9100
    NEWS_PORT = 9200
    
    OPTION_ROOT_ALIASES = {
        "SPX": ["SPXW", "SPX"],
    }

    def __init__(
        self,
        host: Optional[str] = None,
        login: Optional[str] = None,
        password: Optional[str] = None,
        product_id: Optional[str] = None,
        timeout: int = 30,
        level1_port: Optional[int] = None,
        lookup_port: Optional[int] = None,
        admin_port: Optional[int] = None,
        news_port: Optional[int] = None,
        lookup_timeout: Optional[int] = None,
    ):
        super().__init__(timeout=timeout)
        self.host = host or config.iqfeed_host or "127.0.0.1"
        self.admin_port = admin_port or config.iqfeed_port_admin or self.ADMIN_PORT
        self.level1_port = level1_port or config.iqfeed_port_level1 or self.LEVEL1_PORT
        self.lookup_port = lookup_port or config.iqfeed_port_lookup or self.LOOKUP_PORT
        self.history_port = self.lookup_port
        self.news_port = news_port or config.iqfeed_port_news or self.NEWS_PORT
        self.lookup_read_timeout = lookup_timeout or config.iqfeed_lookup_timeout or 12
        self._client_app_name = config.get("IQFEED_CLIENT_APP_NAME", "ZeroDTE-pipeline")
        self._client_version = self._sanitize_credential(config.iqfeed_version) or "ZeroDTE-pipeline/1.0"
        self._autostart_enabled = config.iqfeed_autostart_enabled
        self._headless_login_enabled = config.iqfeed_headless_login_enabled
        
        # Scrub registry values first so manual IQConnect UI stops inheriting stray quotes
        self._clean_registry_credentials()

        # Load credentials with fallback hierarchy:
        # 1. Explicitly passed parameters
        # 2. Environment variables / .env file
        # 3. Windows registry (if available)
        self._login = self._sanitize_credential(
            login or config.iqfeed_login or self._load_registry_credential("Login")
        )
        self._password = self._sanitize_credential(
            password or config.iqfeed_password or self._load_registry_credential("Password")
        )
        self._product_id = self._sanitize_credential(
            product_id or config.iqfeed_product_id or self._load_registry_credential("ProductID")
        )
        
        # Connection state
        self._admin_socket: Optional[socket.socket] = None
        self._level1_socket: Optional[socket.socket] = None
        self._lookup_socket: Optional[socket.socket] = None
        
        # Authentication state - prevents auth loop
        self._auth_attempts = 0
        self._max_auth_attempts = 3
        self._authenticated = False
    
    def _clean_registry_credentials(self) -> None:
        """Remove stray quotes saved by IQFeed UI so future launches stay clean."""
        try:
            import winreg

            registry_paths = [
                r"SOFTWARE\DTN\IQFeed\Startup",
                r"SOFTWARE\WOW6432Node\DTN\IQFeed\Startup",
            ]

            for reg_path in registry_paths:
                try:
                    with winreg.OpenKey(
                        winreg.HKEY_CURRENT_USER,
                        reg_path,
                        0,
                        winreg.KEY_READ | winreg.KEY_SET_VALUE,
                    ) as reg_key:
                        for field in ("Login", "Password", "ProductID"):
                            try:
                                raw_value, _ = winreg.QueryValueEx(reg_key, field)
                            except FileNotFoundError:
                                continue

                            if not isinstance(raw_value, str):
                                continue

                            cleaned = self._sanitize_credential(raw_value)
                            if cleaned != raw_value:
                                winreg.SetValueEx(reg_key, field, 0, winreg.REG_SZ, cleaned or "")
                                logger.info(
                                    "Cleaned IQFeed %s credential in registry path %s",
                                    field,
                                    reg_path,
                                )
                except (FileNotFoundError, OSError):
                    continue
        except ImportError:
            return
        except PermissionError as exc:
            logger.warning("Unable to update IQFeed registry credentials: %s", exc)

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

    @staticmethod
    def _sanitize_credential(value: Optional[str]) -> Optional[str]:
        """Trim whitespace/quotes from credential fields to avoid stray characters."""
        if value is None:
            return None
        return value.strip().strip('"').strip("'")

    @staticmethod
    def _safe_float(value: Optional[str]) -> Optional[float]:
        """Convert strings to float, returning None on blanks or bad values."""
        if value is None:
            return None
        value = value.strip()
        if not value:
            return None
        try:
            return float(value)
        except ValueError:
            return None
    
    async def connect(self) -> bool:
        """Establish connection to IQFeed.
        
        Returns:
            True if connection successful, False otherwise.
        """
        try:
            # Check if IQConnect.exe is running
            if not await self._check_iqconnect_running():
                if self._autostart_enabled:
                    logger.warning("IQConnect.exe not detected, attempting to start")
                    await self._start_iqconnect()
                else:
                    logger.warning(
                        "IQConnect.exe not detected and autostart disabled; please start IQFeed manually"
                    )
                    self._last_error = "IQConnect not running (autostart disabled)"
                    return False
            
            # Connect to admin port first
            self._admin_socket = await self._connect_socket(self.admin_port)
            if not self._admin_socket:
                self._last_error = "Failed to connect to IQFeed admin port"
                return False
            
            # Authenticate
            if not await self._authenticate():
                self._last_error = "Authentication failed"
                return False
            
            # Connect to data ports
            self._level1_socket = await self._connect_socket(self.level1_port)
            self._lookup_socket = await self._connect_socket(self.lookup_port)
            
            if not self._level1_socket or not self._lookup_socket:
                self._last_error = "Failed to connect to IQFeed data ports"
                return False

            # Ensure lookup socket negotiates protocol before use
            await self._initialize_lookup_socket()
            
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
            result = sock.connect_ex((self.host, self.admin_port))
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
            
            if not self._autostart_enabled:
                logger.info("IQFeed autostart disabled; skipping IQConnect launch")
                return

            if not self._headless_login_enabled:
                logger.warning(
                    "IQFeed headless login disabled; cannot launch IQConnect with required credentials"
                )
                return

            missing = [
                label
                for label, value in (
                    ("login", self._login),
                    ("password", self._password),
                    ("product", self._product_id),
                    ("version", self._client_version),
                )
                if not value
            ]
            if missing:
                raise AuthenticationError(
                    f"Missing IQFeed credential fields required for autostart: {', '.join(missing)}"
                )

            args = [
                iqconnect_path,
                f"-login {self._login}",
                f"-password {self._password}",
                f"-product {self._product_id}",
                f"-version {self._client_version}",
                "-autoconnect",
                f"-clientappname {self._client_app_name or 'ZeroDTE-pipeline'}",
                "-port",
                str(self.level1_port),
                str(self.lookup_port),
                str(self.admin_port),
                str(self.news_port),
            ]
            
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

    async def _initialize_lookup_socket(self) -> None:
        """Send required protocol setup commands on the lookup socket."""
        if not self._lookup_socket:
            return

        try:
            await self._drain_socket(self._lookup_socket)

            if self._client_app_name:
                await self._send_command(
                    self._lookup_socket,
                    f"S,SET CLIENT NAME,{self._client_app_name}",
                )
                try:
                    await self._read_response(
                        self._lookup_socket,
                        timeout=min(5, self.lookup_read_timeout),
                    )
                except TimeoutError:
                    logger.debug("Lookup socket did not acknowledge client name")

            for attempt in range(2):
                try:
                    await self._send_command(self._lookup_socket, "S,SET PROTOCOL,6.2")
                    response = await self._read_response(
                        self._lookup_socket,
                        timeout=self.lookup_read_timeout,
                    )
                    if "S,CURRENT PROTOCOL" in response:
                        logger.debug("Lookup socket protocol confirmed at 6.2")
                        return
                except TimeoutError:
                    logger.warning(
                        "Lookup socket protocol negotiation timed out (attempt %s)",
                        attempt + 1,
                    )
            logger.warning("Lookup socket protocol negotiation did not receive confirmation")
        except Exception as exc:
            logger.warning(f"Lookup socket protocol negotiation failed: {exc}")

    async def _drain_socket(self, sock: socket.socket) -> None:
        """Drain any pending greeting data to avoid mixing with commands."""
        loop = asyncio.get_event_loop()
        for _ in range(2):
            try:
                data = await asyncio.wait_for(loop.sock_recv(sock, 4096), timeout=0.25)
                if not data:
                    break
            except asyncio.TimeoutError:
                break
            except Exception:
                break
    
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
            
            numeric: Dict[str, Optional[float]] = {}
            for label, idx in (("bid", 1), ("ask", 2), ("last", 3)):
                raw = fields[idx]
                if not raw:
                    numeric[label] = None
                    continue
                price = self._safe_float(raw)
                if price is None:
                    logger.warning(
                        "IQFeed: bad numeric value %r in %s for %s, skipping quote",
                        raw,
                        label,
                        symbol,
                    )
                    return None
                numeric[label] = price

            volume = None
            if fields[4]:
                try:
                    volume = int(fields[4])
                except ValueError:
                    logger.warning(
                        "IQFeed: bad volume value %r for %s, skipping quote",
                        fields[4],
                        symbol,
                    )
                    return None

            return {
                "symbol": symbol,
                "bid": numeric.get("bid"),
                "ask": numeric.get("ask"),
                "last": numeric.get("last"),
                "volume": volume,
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
        Handles IQFeed peculiarities, such as SPX weeklies trading under the
        SPXW root, by requesting all mapped roots and combining the results.
        """
        if not self._connected or not self._lookup_socket:
            return None
        
        try:
            # Default to 0DTE
            if expiration is None:
                expiration = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
            
            option_roots = self.OPTION_ROOT_ALIASES.get(symbol.upper(), [symbol])
            frames: List[pd.DataFrame] = []
            for root in option_roots:
                frame = await self._fetch_chain_for_root(root, expiration)
                if frame is not None and not frame.empty:
                    frames.append(frame)
            
            if not frames:
                return None
            
            return pd.concat(frames, ignore_index=True)
            
        except Exception as e:
            logger.error(f"Error getting options chain for {symbol}: {e}")
            return None

    async def _fetch_chain_for_root(
        self,
        option_root: str,
        expiration: datetime,
    ) -> Optional[pd.DataFrame]:
        """Fetch options chain rows for a specific IQFeed root."""
        exp_str = expiration.strftime("%Y%m%d")
        await self._send_command(
            self._lookup_socket,
            f"COE,{option_root},{exp_str},,0,0,0"
        )

        options: List[Dict[str, Any]] = []
        while True:
            response = await self._read_response(
                self._lookup_socket,
                timeout=self.lookup_read_timeout,
            )
            if "!ENDMSG!" in response:
                break

            for line in response.strip().split("\n"):
                if not line or line.startswith("E,"):
                    continue
                fields = line.split(",")
                if len(fields) >= 10:
                    strike = self._safe_float(fields[1]) if fields[1] else None
                    if strike is None:
                        logger.warning(
                            "IQFeed: bad strike value %r for %s, skipping option row",
                            fields[1],
                            fields[0],
                        )
                        continue

                    bids = {}
                    for label, idx in (("bid", 2), ("ask", 3), ("last", 4), ("iv", 7), ("delta", 8), ("gamma", 9)):
                        raw = fields[idx]
                        if not raw:
                            bids[label] = None
                            continue
                        val = self._safe_float(raw)
                        if val is None:
                            logger.warning(
                                "IQFeed: bad numeric value %r in %s for %s, skipping option row",
                                raw,
                                label,
                                fields[0],
                            )
                            break
                        bids[label] = val
                    else:
                        options.append({
                            "symbol": fields[0],
                            "strike": strike,
                            "type": "call" if "C" in fields[0] else "put",
                            "expiration": expiration,
                            "bid": bids.get("bid"),
                            "ask": bids.get("ask"),
                            "last": bids.get("last"),
                            "volume": int(fields[5]) if fields[5] else 0,
                            "open_interest": int(fields[6]) if fields[6] else 0,
                            "iv": bids.get("iv"),
                            "delta": bids.get("delta"),
                            "gamma": bids.get("gamma"),
                        })

        if not options:
            return None

        df = pd.DataFrame(options)
        df["option_root"] = option_root
        return df
    
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
                response = await self._read_response(
                    self._lookup_socket,
                    timeout=self.lookup_read_timeout,
                )
                if "!ENDMSG!" in response:
                    break
                
                for line in response.strip().split("\n"):
                    if line.startswith("E,"):
                        continue
                    fields = line.split(",")
                    if len(fields) >= 7:
                        open_px = self._safe_float(fields[1]) if fields[1] else None
                        high_px = self._safe_float(fields[2]) if fields[2] else None
                        low_px = self._safe_float(fields[3]) if fields[3] else None
                        close_px = self._safe_float(fields[4]) if fields[4] else None
                        if None in (open_px, high_px, low_px, close_px):
                            logger.warning(
                                "IQFeed: bad bar data %s, skipping row",
                                line,
                            )
                            continue
                        try:
                            volume = int(fields[5])
                        except ValueError:
                            logger.warning(
                                "IQFeed: bad volume %r in bar data, skipping row",
                                fields[5],
                            )
                            continue

                        bars.append({
                            "timestamp": fields[0],
                            "open": open_px,
                            "high": high_px,
                            "low": low_px,
                            "close": close_px,
                            "volume": volume,
                        })
            
            if not bars:
                return None
            
            df = pd.DataFrame(bars)
            df["timestamp"] = pd.to_datetime(df["timestamp"])
            df.set_index("timestamp", inplace=True)
            return df
            
        except Exception as e:
            logger.error(f"Error getting historical bars for {symbol}: {e}")
            return None
