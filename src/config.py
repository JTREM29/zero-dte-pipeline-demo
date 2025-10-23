"""Centralized configuration using Pydantic Settings.

Reads from environment variables (.env loaded separately in main).
"""
from __future__ import annotations
from pydantic_settings import BaseSettings
from pydantic import Field
from typing import Optional


class Settings(BaseSettings):
    polygon_api_key: Optional[str] = Field(default=None, alias="POLYGON_API_KEY")
    polygon_index_fallback: str = Field(default="SPY", alias="POLYGON_INDEX_FALLBACK")
    openai_api_key: Optional[str] = Field(default=None, alias="OPENAI_API_KEY")
    log_level: str = Field(default="INFO", alias="LOG_LEVEL")
    data_dir: str = Field(default="data", alias="DATA_DIR")
    # Comma-separated default symbols used for watchlists (equities/ETFs). Override via env.
    # Defaults to the core trio requested: SPY, QQQ, IWM
    default_symbols: Optional[str] = Field(default="SPY,QQQ,IWM", alias="DEFAULT_SYMBOLS")  # comma-separated
    bar_interval_sec: float = Field(default=1.0, alias="BAR_INTERVAL_SEC")
    risk_free_rate: float = Field(default=0.0, alias="RISK_FREE_RATE")  # annualized decimal
    # Lottos scalper feature flags
    lottos_enabled: bool = Field(default=False, alias="LOTTOS_ENABLED")
    lottos_clearance_bp: float = Field(default=5.0, alias="LOTTOS_CLEARANCE_BP")

    model_config = {
        "extra": "ignore",
        "case_sensitive": False,
        "env_file_encoding": "utf-8",
    }

    @property
    def has_polygon(self) -> bool:
        return bool(self.polygon_api_key)

    @property
    def has_openai(self) -> bool:
        return bool(self.openai_api_key)

    @property
    def symbols_list(self) -> list[str]:
        if not self.default_symbols:
            return []
        return [s.strip() for s in self.default_symbols.split(",") if s.strip()]
