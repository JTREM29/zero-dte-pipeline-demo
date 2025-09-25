"""Centralized logging setup.

Provides a get_logger() that configures structured console logs once.
"""
from __future__ import annotations
import logging
from functools import lru_cache

LOG_FORMAT = "%(asctime)s | %(levelname)s | %(name)s | %(message)s"


@lru_cache(maxsize=1)
def configure_logging(level: str = "INFO") -> None:
    logging.basicConfig(level=level.upper(), format=LOG_FORMAT)


def get_logger(name: str) -> logging.Logger:
    configure_logging()
    return logging.getLogger(name)
