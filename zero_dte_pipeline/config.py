"""Configuration management for Zero DTE Pipeline.

Loads configuration from .env files and OS environment variables.
"""
import os
from pathlib import Path
from typing import Any, Dict, List, Optional

from dotenv import load_dotenv


class Config:
    """Configuration manager that loads from .env and OS environment."""
    
    _instance: Optional["Config"] = None
    _loaded: bool = False
    
    def __new__(cls) -> "Config":
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance
    
    def __init__(self):
        if not Config._loaded:
            self._load_env()
            Config._loaded = True
    
    def _load_env(self) -> None:
        """Load environment variables from .env files and OS environment."""
        # Try to load from multiple .env locations
        env_paths = [
            Path.cwd() / ".env",
            Path.cwd() / ".env.local",
            Path.home() / ".zero-dte" / ".env",
        ]
        
        for env_path in env_paths:
            if env_path.exists():
                load_dotenv(env_path, override=False)
        
        # OS environment variables take precedence (already set)
    
    def get(self, key: str, default: Optional[str] = None) -> Optional[str]:
        """Get a configuration value."""
        return os.environ.get(key, default)
    
    def get_required(self, key: str) -> str:
        """Get a required configuration value, raising if not found."""
        value = os.environ.get(key)
        if value is None:
            raise ValueError(f"Required configuration '{key}' not found in environment")
        return value
    
    def get_int(self, key: str, default: int = 0) -> int:
        """Get an integer configuration value."""
        value = self.get(key)
        if value is None:
            return default
        try:
            return int(value)
        except ValueError:
            return default
    
    def get_float(self, key: str, default: float = 0.0) -> float:
        """Get a float configuration value."""
        value = self.get(key)
        if value is None:
            return default
        try:
            return float(value)
        except ValueError:
            return default
    
    def get_bool(self, key: str, default: bool = False) -> bool:
        """Get a boolean configuration value."""
        value = self.get(key)
        if value is None:
            return default
        return value.lower() in ("true", "1", "yes", "on")
    
    def get_list(self, key: str, default: Optional[List[str]] = None) -> List[str]:
        """Get a comma-separated list configuration value."""
        value = self.get(key)
        if value is None:
            return default or []
        return [item.strip() for item in value.split(",") if item.strip()]
    
    # IQFeed Configuration
    @property
    def iqfeed_login(self) -> Optional[str]:
        return self.get("IQFEED_LOGIN")
    
    @property
    def iqfeed_password(self) -> Optional[str]:
        return self.get("IQFEED_PASSWORD")
    
    @property
    def iqfeed_product_id(self) -> Optional[str]:
        return self.get("IQFEED_PRODUCT_ID")
    
    # Polygon Configuration
    @property
    def polygon_api_key(self) -> Optional[str]:
        return self.get("POLYGON_API_KEY")
    
    # Alpha Vantage Configuration
    @property
    def alpha_vantage_api_key(self) -> Optional[str]:
        return self.get("ALPHA_VANTAGE_API_KEY")
    
    # OpenAI Configuration
    @property
    def openai_api_key(self) -> Optional[str]:
        return self.get("OPENAI_API_KEY")
    
    # News API Configuration
    @property
    def news_api_key(self) -> Optional[str]:
        return self.get("NEWS_API_KEY")
    
    # Pipeline Settings
    @property
    def default_timeout(self) -> int:
        return self.get_int("DEFAULT_TIMEOUT_SECONDS", 30)
    
    @property
    def candidate_min_confidence(self) -> float:
        return self.get_float("CANDIDATE_MIN_CONFIDENCE", 0.5)
    
    @property
    def gating_strictness(self) -> str:
        return self.get("GATING_STRICTNESS", "moderate")
    
    @property
    def data_source_priority(self) -> List[str]:
        return self.get_list("DATA_SOURCE_PRIORITY", ["iqfeed", "polygon", "alpha_vantage"])
    
    @property
    def log_level(self) -> str:
        return self.get("LOG_LEVEL", "INFO")
    
    @property
    def log_format(self) -> str:
        return self.get("LOG_FORMAT", "json")
    
    def to_dict(self, include_secrets: bool = False) -> Dict[str, Any]:
        """Export configuration as dictionary."""
        config = {
            "data_source_priority": self.data_source_priority,
            "default_timeout": self.default_timeout,
            "candidate_min_confidence": self.candidate_min_confidence,
            "gating_strictness": self.gating_strictness,
            "log_level": self.log_level,
            "log_format": self.log_format,
        }
        
        if include_secrets:
            config.update({
                "iqfeed_login": self.iqfeed_login,
                "iqfeed_password": "***" if self.iqfeed_password else None,
                "polygon_api_key": "***" if self.polygon_api_key else None,
                "alpha_vantage_api_key": "***" if self.alpha_vantage_api_key else None,
                "openai_api_key": "***" if self.openai_api_key else None,
                "news_api_key": "***" if self.news_api_key else None,
            })
        
        return config


# Global config instance
config = Config()
