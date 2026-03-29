"""
File: logging_config.py
Description: Logging configuration with rotating file handler and console output.
             Sets up the root logger with 5 MB rotating files (5 backups) and
             a console stream handler.
Project: config
Notes: Log files are written to logs/smart_locker.log. The logs/ directory is
       created automatically if it does not exist.
"""

import logging
import logging.handlers
from pathlib import Path

# Absolute path to the logs/ directory at the project root
LOG_DIR = Path(__file__).resolve().parent.parent / "logs"


def setup_logging(level: int = logging.INFO) -> None:
    """Configure root logger with rotating file handler and console handler.

    Creates the logs/ directory if it does not exist, then attaches a console
    StreamHandler and a RotatingFileHandler to the root logger so that all
    modules that use ``logging.getLogger(__name__)`` inherit this configuration.

    Args:
        level: Logging severity threshold (default ``logging.INFO``).
               Applied to both the root logger and each handler.

    Returns:
        None.
    """
    LOG_DIR.mkdir(exist_ok=True)

    root = logging.getLogger()
    root.setLevel(level)

    fmt = logging.Formatter(
        "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # Console handler
    console = logging.StreamHandler()
    console.setLevel(level)
    console.setFormatter(fmt)
    root.addHandler(console)

    # Rotating file handler — 5 MB per file, keep 5 backups
    file_handler = logging.handlers.RotatingFileHandler(
        LOG_DIR / "smart_locker.log",
        maxBytes=5 * 1024 * 1024,
        backupCount=5,
        encoding="utf-8",
    )
    file_handler.setLevel(level)
    file_handler.setFormatter(fmt)
    root.addHandler(file_handler)
