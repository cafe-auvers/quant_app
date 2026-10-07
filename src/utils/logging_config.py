"""Application-level logging configuration."""
from __future__ import annotations

import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Optional

from src.utils.config import DATA_DIR, get_env_value


DEFAULT_LOG_FILE = DATA_DIR / "logs" / "quant_app.log"
LOG_FORMAT = "%(asctime)s | %(levelname)s | %(name)s | %(message)s"


def configure_logging(
    *,
    level: Optional[str] = None,
    log_file: Optional[Path] = None,
) -> None:
    """Configure console and rotating-file logs once at an application entry point."""
    level_name = str(level or get_env_value("LOG_LEVEL", "INFO") or "INFO").upper()
    log_level = getattr(logging, level_name, logging.INFO)
    target = Path(log_file or DEFAULT_LOG_FILE)
    target.parent.mkdir(parents=True, exist_ok=True)

    console_handler = logging.StreamHandler(sys.stderr) if sys.stderr is not None else None
    file_handler = RotatingFileHandler(
        target,
        maxBytes=5 * 1024 * 1024,
        backupCount=3,
        encoding="utf-8",
    )
    formatter = logging.Formatter(LOG_FORMAT)
    if console_handler is not None:
        console_handler.setFormatter(formatter)
    file_handler.setFormatter(formatter)

    logging.basicConfig(
        level=log_level,
        handlers=[handler for handler in (console_handler, file_handler) if handler is not None],
        force=True,
    )
