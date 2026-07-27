"""Rotating log setup for Twitch247."""

from __future__ import annotations

import logging
import os
from logging.handlers import RotatingFileHandler
from pathlib import Path


class _OnlyLogger(logging.Filter):
    def __init__(self, logger_name: str) -> None:
        super().__init__()
        self.logger_name = logger_name

    def filter(self, record: logging.LogRecord) -> bool:
        return record.name == self.logger_name


class _ExcludeLogger(logging.Filter):
    def __init__(self, logger_name: str) -> None:
        super().__init__()
        self.logger_name = logger_name

    def filter(self, record: logging.LogRecord) -> bool:
        return record.name != self.logger_name


def setup_logging(log_dir: Path, log_level: str = "INFO") -> logging.Logger:
    log_dir.mkdir(parents=True, exist_ok=True)

    level = getattr(logging, log_level.upper(), logging.INFO)
    fmt = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    root = logging.getLogger("twitch247")
    root.setLevel(level)
    for existing_handler in root.handlers:
        existing_handler.close()
    root.handlers.clear()

    console = logging.StreamHandler()
    console.setFormatter(fmt)
    root.addHandler(console)

    for name, filename in (
        ("main", "twitch247.log"),
        ("error", "error.log"),
        ("playback", "playback.log"),
    ):
        log_path = log_dir / filename
        handler = RotatingFileHandler(
            log_path,
            maxBytes=10 * 1024 * 1024,
            backupCount=5,
            encoding="utf-8",
        )
        os.chmod(log_path, 0o640)
        handler.setFormatter(fmt)
        if name == "error":
            handler.setLevel(logging.ERROR)
        elif name == "playback":
            handler.setLevel(logging.INFO)
            handler.addFilter(_OnlyLogger("twitch247.playback"))
        else:
            handler.setLevel(level)
            handler.addFilter(_ExcludeLogger("twitch247.playback"))
        root.addHandler(handler)

    return root


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(f"twitch247.{name}")
