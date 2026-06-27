"""Rotating log setup for Twitch247."""

from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path


def setup_logging(log_dir: Path, log_level: str = "INFO") -> logging.Logger:
    log_dir.mkdir(parents=True, exist_ok=True)

    level = getattr(logging, log_level.upper(), logging.INFO)
    fmt = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    root = logging.getLogger("twitch247")
    root.setLevel(level)
    root.handlers.clear()

    console = logging.StreamHandler()
    console.setFormatter(fmt)
    root.addHandler(console)

    for name, filename in (
        ("main", "twitch247.log"),
        ("error", "error.log"),
        ("playback", "playback.log"),
    ):
        handler = RotatingFileHandler(
            log_dir / filename,
            maxBytes=10 * 1024 * 1024,
            backupCount=5,
            encoding="utf-8",
        )
        handler.setFormatter(fmt)
        if name == "error":
            handler.setLevel(logging.ERROR)
        elif name == "playback":
            handler.setLevel(logging.INFO)
        else:
            handler.setLevel(level)
        root.addHandler(handler)

    return root


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(f"twitch247.{name}")
