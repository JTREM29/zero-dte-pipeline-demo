"""Tests for configuration management."""
import os
import pytest
from zero_dte_pipeline.config import Config


class TestConfig:
    """Test configuration loading and retrieval."""
    
    def test_config_singleton(self):
        """Config should be a singleton."""
        config1 = Config()
        config2 = Config()
        assert config1 is config2
    
    def test_get_with_default(self):
        """Get should return default when key not found."""
        config = Config()
        result = config.get("NONEXISTENT_KEY_12345", "default_value")
        assert result == "default_value"
    
    def test_get_int(self):
        """get_int should parse integer values."""
        config = Config()
        os.environ["TEST_INT_VALUE"] = "42"
        try:
            result = config.get_int("TEST_INT_VALUE", 0)
            assert result == 42
        finally:
            del os.environ["TEST_INT_VALUE"]
    
    def test_get_int_default(self):
        """get_int should return default for invalid values."""
        config = Config()
        os.environ["TEST_INVALID_INT"] = "not_a_number"
        try:
            result = config.get_int("TEST_INVALID_INT", 99)
            assert result == 99
        finally:
            del os.environ["TEST_INVALID_INT"]
    
    def test_get_float(self):
        """get_float should parse float values."""
        config = Config()
        os.environ["TEST_FLOAT_VALUE"] = "3.14"
        try:
            result = config.get_float("TEST_FLOAT_VALUE", 0.0)
            assert result == 3.14
        finally:
            del os.environ["TEST_FLOAT_VALUE"]
    
    def test_get_bool_true(self):
        """get_bool should parse true values."""
        config = Config()
        for val in ["true", "1", "yes", "on", "True", "YES"]:
            os.environ["TEST_BOOL"] = val
            try:
                result = config.get_bool("TEST_BOOL", False)
                assert result is True, f"Failed for value: {val}"
            finally:
                del os.environ["TEST_BOOL"]
    
    def test_get_bool_false(self):
        """get_bool should parse false values."""
        config = Config()
        os.environ["TEST_BOOL"] = "false"
        try:
            result = config.get_bool("TEST_BOOL", True)
            assert result is False
        finally:
            del os.environ["TEST_BOOL"]
    
    def test_get_list(self):
        """get_list should parse comma-separated values."""
        config = Config()
        os.environ["TEST_LIST"] = "a, b, c"
        try:
            result = config.get_list("TEST_LIST")
            assert result == ["a", "b", "c"]
        finally:
            del os.environ["TEST_LIST"]
    
    def test_to_dict(self):
        """to_dict should return configuration dictionary."""
        config = Config()
        result = config.to_dict()
        
        assert isinstance(result, dict)
        assert "data_source_priority" in result
        assert "default_timeout" in result
        assert "gating_strictness" in result
    
    def test_properties(self):
        """Configuration properties should be accessible."""
        config = Config()
        
        # These should not raise
        _ = config.default_timeout
        _ = config.candidate_min_confidence
        _ = config.data_source_priority
        _ = config.log_level
