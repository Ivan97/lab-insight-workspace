"""Application logging, bounded on disk.

A long-running demo box should not fill its disk with logs, so file output is
size-rotated: at most LOG_MAX_BYTES per file and LOG_BACKUP_COUNT old files,
which caps total usage at a predictable figure reported by describe().

Console output stays on so `make demo` still shows what is happening.
"""

import logging
import logging.handlers
import os
from pathlib import Path
from typing import Any

from .config import ROOT_DIR

LOG_FORMAT = "%(asctime)s %(levelname)-8s %(name)-24s %(message)s"
DEFAULT_MAX_BYTES = 5 * 1024 * 1024
DEFAULT_BACKUP_COUNT = 5

_configured = False


def log_dir() -> Path:
    override = os.getenv("LOG_DIR")
    return Path(override) if override else ROOT_DIR / "logs"


def _int_env(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = int(raw)
    except ValueError:
        raise ValueError(f"{name}={raw!r} must be an integer") from None
    if value <= 0:
        raise ValueError(f"{name}={value} must be positive")
    return value


def max_bytes() -> int:
    return _int_env("LOG_MAX_BYTES", DEFAULT_MAX_BYTES)


def backup_count() -> int:
    return _int_env("LOG_BACKUP_COUNT", DEFAULT_BACKUP_COUNT)


def describe() -> dict[str, Any]:
    """What the health endpoint reports, including the worst-case disk figure."""
    directory = log_dir()
    files = sorted(p.name for p in directory.glob("app.log*")) if directory.is_dir() else []
    return {
        "directory": str(directory),
        "level": os.getenv("LOG_LEVEL", "INFO").upper(),
        "max_bytes": max_bytes(),
        "backup_count": backup_count(),
        # Rotation keeps the active file plus backup_count older ones.
        "max_total_bytes": max_bytes() * (backup_count() + 1),
        "files": files,
    }


def configure_logging() -> None:
    """Attach console and rotating file handlers once."""
    global _configured
    if _configured:
        return
    level = getattr(logging, os.getenv("LOG_LEVEL", "INFO").upper(), logging.INFO)
    directory = log_dir()
    directory.mkdir(parents=True, exist_ok=True)

    formatter = logging.Formatter(LOG_FORMAT)
    file_handler = logging.handlers.RotatingFileHandler(
        directory / "app.log",
        maxBytes=max_bytes(),
        backupCount=backup_count(),
        encoding="utf-8",
    )
    file_handler.setFormatter(formatter)
    console = logging.StreamHandler()
    console.setFormatter(formatter)

    root = logging.getLogger()
    root.setLevel(level)
    root.handlers = [console, file_handler]

    # uvicorn installs its own handlers; routing them through root sends access
    # and error logs to the same rotated file as everything else.
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        logger = logging.getLogger(name)
        logger.handlers = []
        logger.propagate = True

    # These are chatty at DEBUG and would rotate useful lines away.
    for name in ("httpx", "httpcore", "openai", "urllib3"):
        logging.getLogger(name).setLevel(max(level, logging.WARNING))

    _configured = True
