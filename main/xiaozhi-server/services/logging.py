from __future__ import annotations

import os
import sys
from typing import Optional

from loguru import logger as _loguru_logger

_LOGGER = None


def _configure_fallback(exc: Optional[Exception] = None):
    """Configure a minimal console logger when config.logger is unavailable."""
    level = os.environ.get("LOG_LEVEL", "INFO")
    log_format = os.environ.get(
        "LOG_FORMAT",
        "<green>{time:YYYY-MM-DD HH:mm:ss}</green> | "
        "{level:<8} | {extra[tag]} | {message}",
    )
    _loguru_logger.remove()
    _loguru_logger.add(
        sys.stdout,
        format=log_format,
        level=level,
        enqueue=True,
    )
    if exc and os.environ.get("LOG_FALLBACK_DEBUG"):
        _loguru_logger.warning(
            f"Falling back to minimal logger configuration: {exc}"
        )
    return _loguru_logger


def setup_logging():
    """
    Return a logger instance that works both inside the monolith and in
    isolated environments (e.g., Cloud Functions).
    """
    global _LOGGER
    if _LOGGER is not None:
        return _LOGGER

    try:
        from config.logger import setup_logging as config_setup_logging
    except Exception as exc:
        _LOGGER = _configure_fallback(exc)
        return _LOGGER

    try:
        _LOGGER = config_setup_logging()
    except Exception as exc:
        _LOGGER = _configure_fallback(exc)
    return _LOGGER

