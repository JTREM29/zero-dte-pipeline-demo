"""Centralized logging setup.

Provides helper functions to obtain configured loggers.
- setup_logger(name, level)
- get_logger(name)

Adds both console and rotating file handlers only once per logger.
Global root logger remains untouched unless explicitly configured elsewhere.
"""
from __future__ import annotations
import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path
import os
from functools import lru_cache
from typing import Optional

DEFAULT_CONSOLE_FORMAT = "[%(asctime)s] %(levelname)s %(name)s: %(message)s"
DEFAULT_FILE_FORMAT = "%(asctime)s %(levelname)s %(name)s %(filename)s:%(lineno)d - %(message)s"


def _coerce_level(level: str | int | None) -> int:
    if isinstance(level, int):
        return level
    if not level:
        return logging.INFO
    return getattr(logging, str(level).upper(), logging.INFO)


def setup_logger(name: str = "app", level: str | int | None = None, log_dir: Optional[str] = None) -> logging.Logger:
    """Return a logger with console + rotating file handlers.

    Handlers are added only once (idempotent). The log file is named `<name>.log`.
    LOG_DIR env var or provided log_dir determines directory (defaults to 'logs').
    """
    lvl = _coerce_level(level or os.getenv("LOG_LEVEL", "INFO"))
    base_dir = Path(log_dir or os.getenv("LOG_DIR", "logs"))
    base_dir.mkdir(parents=True, exist_ok=True)

    logger = logging.getLogger(name)
    logger.setLevel(lvl)

    if not logger.handlers:
        ch = logging.StreamHandler()
        ch.setLevel(lvl)
        ch.setFormatter(logging.Formatter(DEFAULT_CONSOLE_FORMAT))

        fh = RotatingFileHandler(base_dir / f"{name}.log", maxBytes=2_000_000, backupCount=3)
        fh.setLevel(lvl)
        fh.setFormatter(logging.Formatter(DEFAULT_FILE_FORMAT))

        logger.addHandler(ch)
        logger.addHandler(fh)
    return logger


@lru_cache(maxsize=128)
def get_logger(name: str) -> logging.Logger:
    """Cached retrieval of a configured logger."""
    return setup_logger(name)
