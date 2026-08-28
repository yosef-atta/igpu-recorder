"""Minimal structured logging configuration."""

import logging
import sys
from typing import TextIO


def setup_logging(
    level: int = logging.INFO,
    stream: TextIO | None = None,
) -> logging.Logger:
    """Configure and return root logger for igpu_recorder.

    Args:
        level: Logging level (e.g. logging.DEBUG, logging.INFO).
        stream: Output stream (defaults to sys.stderr).

    Returns:
        Configured logger instance.
    """
    logger = logging.getLogger("igpu_recorder")
    logger.setLevel(level)

    # Avoid duplicate handlers if setup_logging is called multiple times
    if not logger.handlers:
        handler = logging.StreamHandler(stream or sys.stderr)
        formatter = logging.Formatter(
            fmt="%(asctime)s [%(levelname)s] [%(name)s] %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
        handler.setFormatter(formatter)
        logger.addHandler(handler)

    return logger


def get_logger(name: str | None = None) -> logging.Logger:
    """Retrieve an igpu_recorder logger or sub-logger."""
    if name is None:
        return logging.getLogger("igpu_recorder")
    return logging.getLogger(f"igpu_recorder.{name}")
