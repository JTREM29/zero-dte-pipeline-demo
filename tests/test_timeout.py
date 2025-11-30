"""Tests for timeout utilities."""
import asyncio
import pytest

from zero_dte_pipeline.utils.timeout import (
    with_timeout,
    TimeoutWrapper,
    safe_gather,
)


class TestWithTimeout:
    """Test async timeout wrapper."""
    
    @pytest.mark.asyncio
    async def test_completes_within_timeout(self):
        """Fast operations should complete."""
        async def fast_op():
            return "success"
        
        result = await with_timeout(
            fast_op(),
            timeout=5,
            default="failed",
        )
        
        assert result == "success"
    
    @pytest.mark.asyncio
    async def test_returns_default_on_timeout(self):
        """Slow operations should return default."""
        async def slow_op():
            await asyncio.sleep(10)
            return "success"
        
        result = await with_timeout(
            slow_op(),
            timeout=1,
            default="timed_out",
        )
        
        assert result == "timed_out"
    
    @pytest.mark.asyncio
    async def test_returns_default_on_error(self):
        """Erroring operations should return default."""
        async def error_op():
            raise ValueError("test error")
        
        result = await with_timeout(
            error_op(),
            timeout=5,
            default="error_default",
        )
        
        assert result == "error_default"


class TestTimeoutWrapper:
    """Test timeout context manager."""
    
    @pytest.mark.asyncio
    async def test_wrap_fast_operation(self):
        """Wrap should work for fast operations."""
        wrapper = TimeoutWrapper(timeout=5)
        
        async def fast_op():
            return "success"
        
        result = await wrapper.wrap(fast_op())
        
        assert result == "success"
        assert not wrapper.timed_out
    
    @pytest.mark.asyncio
    async def test_wrap_slow_operation(self):
        """Wrap should handle slow operations."""
        wrapper = TimeoutWrapper(timeout=1)
        
        async def slow_op():
            await asyncio.sleep(10)
            return "success"
        
        result = await wrapper.wrap(slow_op())
        
        assert result is None  # Default


class TestSafeGather:
    """Test safe gather function."""
    
    @pytest.mark.asyncio
    async def test_all_succeed(self):
        """All successful operations should return results."""
        async def op1():
            return 1
        
        async def op2():
            return 2
        
        async def op3():
            return 3
        
        results = await safe_gather(op1(), op2(), op3(), timeout=5)
        
        assert results == [1, 2, 3]
    
    @pytest.mark.asyncio
    async def test_some_fail(self):
        """Failed operations should return None without failing others."""
        async def op1():
            return 1
        
        async def op2():
            raise ValueError("test error")
        
        async def op3():
            return 3
        
        results = await safe_gather(op1(), op2(), op3(), timeout=5)
        
        assert results[0] == 1
        assert results[1] is None  # Failed
        assert results[2] == 3
    
    @pytest.mark.asyncio
    async def test_some_timeout(self):
        """Timed out operations should return None."""
        async def fast():
            return 1
        
        async def slow():
            await asyncio.sleep(10)
            return 2
        
        results = await safe_gather(fast(), slow(), timeout=1)
        
        assert results[0] == 1
        assert results[1] is None  # Timed out
