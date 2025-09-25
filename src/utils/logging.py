"""Convenience logging module.

Exports setup_logger and get_logger from logging_setup for simpler import paths.
"""
from __future__ import annotations
from .logging_setup import get_logger, setup_logger  # re-export

__all__ = ["get_logger", "setup_logger"]
