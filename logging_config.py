"""
Centralized logging configuration.

Replaces the previous ad-hoc `print(...)` calls scattered across the codebase
with a standard `logging` setup that supports levels (INFO/WARNING/ERROR),
consistent formatting, and a single place to control verbosity via the
LOG_LEVEL environment variable.

Usage:
    from logging_config import get_logger
    logger = get_logger(__name__)
    logger.info("...")
    logger.warning("...")
    logger.error("...")
"""
import logging
import os
import sys

_CONFIGURED = False


def configure_logging():
    """
    Configure the root logger once. Safe to call multiple times (idempotent) -
    useful because both bot.py and dashboard.py can be entry points.
    """
    global _CONFIGURED
    if _CONFIGURED:
        return

    level_name = os.getenv("LOG_LEVEL", "INFO").upper()
    level = getattr(logging, level_name, logging.INFO)

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter(
        fmt="%(asctime)s [%(levelname)s] [%(name)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    ))

    root = logging.getLogger()
    root.setLevel(level)
    # Avoid duplicate handlers if configure_logging() runs more than once.
    root.handlers = [handler]

    _CONFIGURED = True


def get_logger(name):
    """
    Returns a module-level logger, ensuring logging is configured first.
    """
    configure_logging()
    return logging.getLogger(name)
