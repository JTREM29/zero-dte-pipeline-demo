"""Centralized logging setup.

Provides a get_logger() that configures structured console logs once.
"""
from __future__ import annotations
import logging
from logging.handlers import RotatingFileHandler
from functools import lru_cache
from pathlib import Path
import os

LOG_FORMAT = "%(asctime)s | %(levelname)s | %(name)s | %(message)s"


@lru_cache(maxsize=1)
def configure_logging(level: str = "INFO") -> None:
    root = logging.getLogger()
    if root.handlers:
        return  # already configured
    root.setLevel(level.upper())

    formatter = logging.Formatter(LOG_FORMAT)

    console = logging.StreamHandler()
    console.setFormatter(formatter)
    root.addHandler(console)

    log_dir = Path(os.getenv("LOG_DIR", "logs"))
    log_dir.mkdir(parents=True, exist_ok=True)
    file_handler = RotatingFileHandler(log_dir / "pipeline.log", maxBytes=1_000_000, backupCount=3)
    file_handler.setFormatter(formatter)
    root.addHandler(file_handler)


def get_logger(name: str) -> logging.Logger:
    configure_logging()
    return logging.getLogger(name)
