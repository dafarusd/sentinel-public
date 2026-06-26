"""Logging setup for Sentinel daemons.

Each daemon gets its own rotating log file at {log_dir}/{name}.log.
"""

from __future__ import annotations

import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

from sentinel.config import get_config


def setup_logging(name: str, log_dir: Path | None = None) -> logging.Logger:
    """Configure and return a logger for a Sentinel daemon.

    Args:
        name: Daemon name (e.g. 'wifi', 'ingest'). Used as log filename.
        log_dir: Override log directory. If None, uses config.

    Returns:
        Configured logger instance.
    """
    cfg = get_config()
    if log_dir is None:
        log_dir = cfg.resolved_log_dir

    log_dir.mkdir(parents=True, exist_ok=True)

    logger = logging.getLogger(f"sentinel.{name}")
    logger.setLevel(getattr(logging, cfg.logging.level.upper(), logging.INFO))

    # Avoid duplicate handlers on reload
    if logger.handlers:
        return logger

    formatter = logging.Formatter(
        "%(asctime)s [%(name)s] %(levelname)s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )

    # File handler with rotation
    log_file = log_dir / f"{name}.log"
    file_handler = RotatingFileHandler(
        str(log_file),
        maxBytes=cfg.logging.max_bytes,
        backupCount=cfg.logging.backup_count,
    )
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)

    # Stderr handler for systemd journal capture
    stderr_handler = logging.StreamHandler(sys.stderr)
    stderr_handler.setFormatter(formatter)
    logger.addHandler(stderr_handler)

    return logger
