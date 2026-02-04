"""Configuration management for Zero DTE Pipeline.

Loads configuration from .env files and OS environment variables.
"""
import json
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
        self._gating_overrides_cache: Optional[Dict[str, Any]] = None
    
    def _load_env(self) -> None:
        """Load environment variables from .env files and OS environment."""
        # Try to load from multiple .env locations
        env_path = Path.cwd() / ".env"
        if env_path.exists():
            load_dotenv(env_path, override=False)

        env_local_path = Path.cwd() / ".env.local"
        if env_local_path.exists():
            # Local runtime config should win over inherited vars.
            load_dotenv(env_local_path, override=True)

        home_env = Path.home() / ".zero-dte" / ".env"
        if home_env.exists():
            # Treat as a fallback (do not override explicit config).
            load_dotenv(home_env, override=False)
        
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
        return self.get("IQFEED_PRODUCT_ID") or self.get("IQFEED_PRODUCT")
    
    @property
    def iqfeed_host(self) -> str:
        return self.get("IQFEED_HOST", "127.0.0.1")
    
    @property
    def iqfeed_port_level1(self) -> int:
        return self.get_int("IQFEED_PORT_LEVEL1", 5009)
    
    @property
    def iqfeed_port_lookup(self) -> int:
        return self.get_int("IQFEED_PORT_LOOKUP", 9100)
    
    @property
    def iqfeed_port_admin(self) -> int:
        return self.get_int("IQFEED_PORT_ADMIN", 9300)
    
    @property
    def iqfeed_port_news(self) -> int:
        return self.get_int("IQFEED_PORT_NEWS", 9200)

    @property
    def iqfeed_lookup_timeout(self) -> int:
        return self.get_int("IQFEED_LOOKUP_TIMEOUT_SECONDS", 12)

    @property
    def iqfeed_news_sources(self) -> List[str]:
        """Preferred IQFeed news providers (e.g., THEFLY)."""
        return self.get_list("IQFEED_NEWS_SOURCES", ["THEFLY"])

    @property
    def iqfeed_autostart_enabled(self) -> bool:
        """Whether the pipeline may spawn IQConnect automatically."""
        return self.get_bool("IQFEED_AUTOSTART", True)

    @property
    def iqfeed_headless_login_enabled(self) -> bool:
        """Whether IQConnect should receive login credentials via CLI args."""
        return self.get_bool("IQFEED_HEADLESS_LOGIN", True)

    @property
    def iqfeed_version(self) -> Optional[str]:
        """Client version string passed to IQConnect."""
        return self.get("IQFEED_VERSION")
    
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

    @property
    def openai_model_name(self) -> str:
        return self.get("OPENAI_MODEL_NAME", "gpt-4.1-mini")

    @property
    def openai_timeout_seconds(self) -> float:
        return self.get_float("OPENAI_TIMEOUT_SECONDS", 20.0)

    @property
    def openai_enabled(self) -> bool:
        return bool(self.openai_api_key)

    # Massive / IBKR Configuration
    @property
    def massive_api_key(self) -> str:
        polygon_key = self.polygon_api_key
        if polygon_key:
            return polygon_key
        return self.get_required("MASSIVE_API_KEY")

    @property
    def massive_base_url(self) -> str:
        raw_value = self.get("MASSIVE_BASE_URL")
        if raw_value:
            return raw_value.strip("'\"")

        if self.polygon_api_key:
            return "https://api.polygon.io"

        return "https://api.massive.com"

    @property
    def ibkr_host(self) -> str:
        return self.get("IBKR_HOST", "127.0.0.1")

    @property
    def ibkr_port(self) -> int:
        return self.get_int("IBKR_PORT", 7497)

    @property
    def ibkr_client_id(self) -> int:
        return self.get_int("IBKR_CLIENT_ID", 1)

    @property
    def es_futures_symbol(self) -> str:
        return self.get("ES_FUTURES_SYMBOL", "ES=F")
    
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
        return self.get("GATING_STRICTNESS", "loose")
    
    @property
    def data_source_priority(self) -> List[str]:
        return self.get_list("DATA_SOURCE_PRIORITY", ["iqfeed", "polygon", "alpha_vantage"])

    @property
    def lazy_data_sources(self) -> List[str]:
        return self.get_list("DATA_SOURCE_LAZY", ["iqfeed"])

    @property
    def morning_report_timeout_seconds(self) -> float:
        return self.get_float("MORNING_REPORT_TIMEOUT_SECONDS", 60.0)

    @property
    def max_morning_candidates(self) -> int:
        return self.get_int("MAX_MORNING_CANDIDATES", 40)

    @property
    def morning_snapshot_timeout_seconds(self) -> float:
        custom = self.get("IQFEED_SNAPSHOT_TIMEOUT_SEC")
        if custom:
            try:
                return float(custom)
            except ValueError:
                pass
        return self.get_float("MORNING_SNAPSHOT_TIMEOUT_SECONDS", 10.0)

    @property
    def options_chain_timeout_seconds(self) -> float:
        custom = self.get("CHAIN_TIMEOUT_SEC")
        if custom:
            try:
                return float(custom)
            except ValueError:
                pass
        return self.get_float("OPTIONS_CHAIN_TIMEOUT_SECONDS", 60.0)

    @property
    def options_chain_fallback_timeout_seconds(self) -> float:
        custom = self.get("CHAIN_FALLBACK_TIMEOUT_SEC") or self.get("POLYGON_CHAIN_TIMEOUT_SEC")
        if custom:
            try:
                return float(custom)
            except ValueError:
                pass
        fallback = self.get_float("OPTIONS_CHAIN_FALLBACK_TIMEOUT_SECONDS", 90.0)
        return max(fallback, self.options_chain_timeout_seconds)

    @property
    def discord_webhook_url(self) -> str:
        return self.get("DISCORD_WEBHOOK_URL", "")
    
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
            "openai_model_name": self.openai_model_name,
            "openai_timeout_seconds": self.openai_timeout_seconds,
            "morning_report_timeout_seconds": self.morning_report_timeout_seconds,
            "max_morning_candidates": self.max_morning_candidates,
            "morning_snapshot_timeout_seconds": self.morning_snapshot_timeout_seconds,
        }
        
        if include_secrets:
            config.update({
                "iqfeed_login": self.iqfeed_login,
                "iqfeed_password": "***" if self.iqfeed_password else None,
                "polygon_api_key": "***" if self.polygon_api_key else None,
                "alpha_vantage_api_key": "***" if self.alpha_vantage_api_key else None,
                "openai_api_key": "***" if self.openai_api_key else None,
                "news_api_key": "***" if self.news_api_key else None,
                "discord_webhook_url": "***" if self.discord_webhook_url else None,
            })
        
        return config

    def gating_overrides(self) -> Dict[str, Any]:
        """Load optional gating overrides defined via JSON."""
        if self._gating_overrides_cache is not None:
            return self._gating_overrides_cache

        overrides = self._load_gating_overrides()
        self._gating_overrides_cache = overrides
        return overrides

    def _load_gating_overrides(self) -> Dict[str, Any]:
        """Read gating overrides from configured paths, if present."""
        path_candidates: List[Path] = []
        env_path = self.get("GATING_OVERRIDES_PATH") or self.get("GATING_RULES_PATH")
        if env_path:
            path_candidates.append(Path(env_path).expanduser())

        path_candidates.append(Path.cwd() / "config" / "gating_rules.json")
        path_candidates.append(Path.cwd() / "gating_rules.json")

        for candidate in path_candidates:
            if not candidate or not candidate.exists():
                continue
            try:
                with candidate.open("r", encoding="utf-8") as handle:
                    data = json.load(handle)
                if isinstance(data, dict):
                    return data
            except Exception:
                continue
        return {}


# Global config instance
config = Config()
