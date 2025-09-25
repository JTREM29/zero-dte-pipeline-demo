"""Lightweight Settings dataclass wrapper around environment variables.

Note: The project already has `src.config.Settings` using pydantic-settings. This module
provides a simpler alternative for quick scripts. Prefer the pydantic version for
validation and richer features.
"""
from __future__ import annotations
import os
from dataclasses import dataclass
try:
    from dotenv import load_dotenv  # type: ignore
    load_dotenv(override=False)
except Exception:  # pragma: no cover
    pass


@dataclass(frozen=True)
class SettingsLite:
    openai_api_key: str = os.getenv("OPENAI_API_KEY", "")
    iqfeed_host: str = os.getenv("IQFEED_HOST", "127.0.0.1")
    iqfeed_port_level1: int = int(os.getenv("IQFEED_PORT_LEVEL1", "5009"))
    iqfeed_port_admin: int = int(os.getenv("IQFEED_PORT_ADMIN", "5009"))
    polygon_api_key: str = os.getenv("POLYGON_API_KEY", "")


settings = SettingsLite()
