"""Centralized configuration using Pydantic Settings.

Reads from environment variables (.env loaded separately in main).
"""
from __future__ import annotations
from pydantic_settings import BaseSettings
from pydantic import Field
from typing import Optional


class Settings(BaseSettings):
    polygon_api_key: Optional[str] = Field(default=None, alias="POLYGON_API_KEY")
    openai_api_key: Optional[str] = Field(default=None, alias="OPENAI_API_KEY")
    log_level: str = Field(default="INFO", alias="LOG_LEVEL")
    data_dir: str = Field(default="data", alias="DATA_DIR")

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
