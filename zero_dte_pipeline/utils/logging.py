"""Logging utilities for Zero DTE Pipeline.

Provides structured JSON logging with configurable output.
"""
import json
import logging
import sys
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from zero_dte_pipeline.config import config


class JSONFormatter(logging.Formatter):
    """JSON log formatter for structured logging."""
    
    def format(self, record: logging.LogRecord) -> str:
        log_data: Dict[str, Any] = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        
        # Add exception info if present
        if record.exc_info:
            log_data["exception"] = self.formatException(record.exc_info)
        
        # Add extra fields
        if hasattr(record, "extra_fields"):
            log_data.update(record.extra_fields)
        
        return json.dumps(log_data)


class StructuredLogger(logging.Logger):
    """Logger with structured logging support."""
    
    def _log_with_extra(
        self,
        level: int,
        msg: str,
        *args,
        extra_fields: Optional[Dict[str, Any]] = None,
        **kwargs,
    ):
        """Log with extra structured fields."""
        if extra_fields:
            extra = kwargs.get("extra", {})
            extra["extra_fields"] = extra_fields
            kwargs["extra"] = extra
        super()._log(level, msg, args, **kwargs)
    
    def debug_json(self, msg: str, **fields):
        """Debug log with JSON fields."""
        self._log_with_extra(logging.DEBUG, msg, extra_fields=fields)
    
    def info_json(self, msg: str, **fields):
        """Info log with JSON fields."""
        self._log_with_extra(logging.INFO, msg, extra_fields=fields)
    
    def warning_json(self, msg: str, **fields):
        """Warning log with JSON fields."""
        self._log_with_extra(logging.WARNING, msg, extra_fields=fields)
    
    def error_json(self, msg: str, **fields):
        """Error log with JSON fields."""
        self._log_with_extra(logging.ERROR, msg, extra_fields=fields)


# Set custom logger class
logging.setLoggerClass(StructuredLogger)


def get_logger(name: str) -> StructuredLogger:
    """Get a configured logger.
    
    Args:
        name: Logger name (typically __name__)
        
    Returns:
        Configured logger instance.
    """
    logger = logging.getLogger(name)
    
    if not logger.handlers:
        # Configure handler
        handler = logging.StreamHandler(sys.stdout)
        
        # Set formatter based on config
        if config.log_format == "json":
            handler.setFormatter(JSONFormatter())
        else:
            handler.setFormatter(
                logging.Formatter(
                    "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
                )
            )
        
        logger.addHandler(handler)
        
        # Set level
        level = getattr(logging, config.log_level.upper(), logging.INFO)
        logger.setLevel(level)
    
    return logger


def create_debug_output(
    operation: str,
    status: str,
    details: Optional[Dict[str, Any]] = None,
    metrics: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Create a standardized debug output dictionary.
    
    Args:
        operation: Name of the operation
        status: Status (success, error, warning)
        details: Additional details
        metrics: Performance metrics
        
    Returns:
        Structured debug output dictionary.
    """
    output = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "operation": operation,
        "status": status,
    }
    
    if details:
        output["details"] = details
    
    if metrics:
        output["metrics"] = metrics
    
    return output
