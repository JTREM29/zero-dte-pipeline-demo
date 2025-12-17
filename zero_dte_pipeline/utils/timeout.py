"""Timeout utilities for network operations.

Provides timeout wrappers to prevent stalling on external calls.
"""
import asyncio
import functools
import threading
import _thread
from typing import Any, Callable, Optional, TypeVar

from zero_dte_pipeline.config import config
from zero_dte_pipeline.utils.logging import get_logger

logger = get_logger(__name__)

T = TypeVar("T")


class TimeoutError(Exception):
    """Operation timed out."""
    pass


class Timeout:
    """Context manager that interrupts the main thread after a deadline.

    Uses ``threading.Timer`` plus ``_thread.interrupt_main`` so it works on
    both Windows and Unix platforms without relying on ``signal.alarm``.
    """

    def __init__(self, seconds: float, operation_name: str = "operation") -> None:
        self.seconds = max(0.0, float(seconds))
        self.operation_name = operation_name
        self._timer: Optional[threading.Timer] = None
        self._timed_out = False

    def _trigger(self) -> None:
        self._timed_out = True
        logger.warning("Timeout after %.2fs for %s", self.seconds, self.operation_name)
        _thread.interrupt_main()

    def __enter__(self) -> "Timeout":
        if self.seconds > 0:
            self._timer = threading.Timer(self.seconds, self._trigger)
            self._timer.daemon = True
            self._timer.start()
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        if self._timer:
            self._timer.cancel()

        if not self._timed_out:
            return False

        raise TimeoutError(f"Timeout after {self.seconds:.2f}s for {self.operation_name}") from None


async def with_timeout(
    coro,
    timeout: Optional[int] = None,
    default: Any = None,
    operation_name: str = "operation",
) -> Any:
    """Execute a coroutine with timeout and safe fallback.
    
    Args:
        coro: The coroutine to execute
        timeout: Timeout in seconds (defaults to config value)
        default: Default value to return on timeout
        operation_name: Name for logging purposes
        
    Returns:
        Result of coroutine or default value on timeout.
    """
    timeout = timeout or config.default_timeout
    
    try:
        return await asyncio.wait_for(coro, timeout=timeout)
    except asyncio.TimeoutError:
        logger.warning(f"Timeout after {timeout}s for {operation_name}")
        return default
    except Exception as e:
        logger.error(f"Error in {operation_name}: {e}")
        return default


def sync_with_timeout(
    timeout: Optional[int] = None,
    default: Any = None,
):
    """Decorator to add timeout to synchronous functions.
    
    Uses threading for synchronous operations.
    
    Args:
        timeout: Timeout in seconds
        default: Default value on timeout
    """
    import concurrent.futures
    
    def decorator(func: Callable[..., T]) -> Callable[..., T]:
        @functools.wraps(func)
        def wrapper(*args, **kwargs) -> T:
            nonlocal timeout
            timeout = timeout or config.default_timeout
            
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
                future = executor.submit(func, *args, **kwargs)
                try:
                    return future.result(timeout=timeout)
                except concurrent.futures.TimeoutError:
                    logger.warning(f"Timeout after {timeout}s for {func.__name__}")
                    return default
        
        return wrapper
    
    return decorator


class TimeoutWrapper:
    """Context manager for operations with timeout.
    
    Can be used for both sync and async operations.
    """
    
    def __init__(
        self,
        timeout: Optional[int] = None,
        operation_name: str = "operation",
    ):
        self.timeout = timeout or config.default_timeout
        self.operation_name = operation_name
        self._timed_out = False
    
    @property
    def timed_out(self) -> bool:
        return self._timed_out
    
    async def __aenter__(self):
        return self
    
    async def __aexit__(self, exc_type, exc_val, exc_tb):
        if exc_type is asyncio.TimeoutError:
            self._timed_out = True
            logger.warning(f"Timeout for {self.operation_name}")
            return True  # Suppress exception
        return False
    
    def wrap(self, coro):
        """Wrap a coroutine with timeout."""
        return with_timeout(
            coro,
            timeout=self.timeout,
            operation_name=self.operation_name,
        )


async def safe_gather(*coros, timeout: Optional[int] = None) -> list:
    """Gather coroutines with individual timeouts and safe error handling.
    
    Unlike asyncio.gather, this won't fail completely if one task fails.
    
    Args:
        *coros: Coroutines to execute
        timeout: Timeout for each coroutine
        
    Returns:
        List of results (None for failed/timed out operations).
    """
    timeout = timeout or config.default_timeout
    
    async def safe_run(coro, index: int):
        try:
            return await asyncio.wait_for(coro, timeout=timeout)
        except asyncio.TimeoutError:
            logger.warning(f"Task {index} timed out")
            return None
        except Exception as e:
            logger.error(f"Task {index} failed: {e}")
            return None
    
    tasks = [safe_run(coro, i) for i, coro in enumerate(coros)]
    return await asyncio.gather(*tasks)
