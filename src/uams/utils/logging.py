"""Structured logging utilities for UAMS.

All modules should use `get_logger(__name__)` to obtain a logger.
Production deployments can switch to JSON formatting via `UAMS_LOG_FORMAT=json`.
"""

import logging
import sys
import os


def configure_logging(level: str = "INFO", structured: bool = True) -> None:
    """Configure root logging for UAMS.

    Args:
        level: Log level (DEBUG, INFO, WARNING, ERROR, CRITICAL).
        structured: If True, use JSON format when possible (production).
                   If False, use plain text (development).
    """
    log_format = os.getenv("UAMS_LOG_FORMAT", "json" if structured else "text")
    if log_format == "json":
        fmt = '{"time":"%(asctime)s","level":"%(levelname)s","logger":"%(name)s","msg":"%(message)s","module":"%(module)s","func":"%(funcName)s","line":%(lineno)d}'
    else:
        fmt = "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s"

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter(fmt))

    root = logging.getLogger("uams")
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(getattr(logging, level.upper(), logging.INFO))

    # Silence noisy third-party libraries if needed
    logging.getLogger("chromadb").setLevel(logging.WARNING)


def get_logger(name: str) -> logging.Logger:
    """Get a UAMS logger."""
    return logging.getLogger(f"uams.{name}")
