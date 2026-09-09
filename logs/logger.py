"""Loguru logging configuration (console + rotating file sinks)."""

import os
import sys
import time
from pathlib import Path
from loguru import logger
from dotenv import load_dotenv
from config import ROOT_DIR, ENV_PATH
from config.sherry_settings import get_sherry_setting

load_dotenv(ENV_PATH, override=True)


def _clean_expired_logs(log_dir: Path, timeout_days: int = 7):
    """Recursively delete log files older than `days` under `log_dir`.

    Walks ``log_dir`` including type sub-directories (``info/``, ``error/``)
    so archived logs in every per-type folder are pruned.
    """
    cutoff = time.time() - timeout_days * 86400
    if not log_dir.is_dir():
        return
    for p in log_dir.rglob("*"):
        if p.is_file() and p.suffix in (".log", ".zip") and p.stat().st_mtime < cutoff:
            p.unlink(missing_ok=True)


def _resolve_level() -> str:
    """Read LOG_LEVEL from sherry.jsonc, fall back to INFO on invalid values."""
    raw = str(get_sherry_setting("LOG_LEVEL")).strip().upper()
    valid = {"TRACE", "DEBUG", "INFO", "SUCCESS", "WARNING", "ERROR", "CRITICAL"}
    return raw if raw in valid else "INFO"


def init_logger(log_dir=ROOT_DIR / "logs/output", timeout_days: int = 7):
    """Initialize the global logging configuration."""
    # Create per-type log sub-directories: info/, error/, and all/ (everything)
    log_dir = Path(log_dir)
    info_dir = log_dir / "info"
    error_dir = log_dir / "error"
    all_dir = log_dir / "all"
    for d in (log_dir, info_dir, error_dir, all_dir):
        if not os.path.exists(d):
            os.makedirs(d)

    # 0. Delete expired logs (walk each per-type sub-directory recursively)
    _clean_expired_logs(log_dir, timeout_days)

    level = _resolve_level()

    # 1. Clear Loguru's default configuration
    logger.remove()

    # 2. Console output
    logger.add(
        sys.stderr,
        level=level,
        format="<green>{time:YYYY-MM-DD HH:mm:ss.SSS}</green> | <level>{level: <8}</level> | <cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> - <level>{message}</level>",
        enqueue=True,
    )

    # 3. Normal activity log (captures INFO only for day-to-day review;
    #    WARNING/ERROR/CRITICAL go to the error log, TRACE/DEBUG to the full log)
    logger.add(
        os.path.join(info_dir, f"info_{{time:YYYY-MM-DD}}_{os.getpid()}.log"),
        level="INFO",
        filter=lambda record: record["level"].name == "INFO",
        rotation="100 MB",
        retention="30 days",
        compression="zip",
        encoding="utf-8",
        enqueue=True,
    )

    # 4. Full log (records every level, including TRACE/DEBUG/WARNING, for thorough troubleshooting)
    logger.add(
        os.path.join(all_dir, f"all_{{time:YYYY-MM-DD}}_{os.getpid()}.log"),
        level="TRACE",
        rotation="100 MB",
        retention="30 days",
        compression="zip",
        encoding="utf-8",
        enqueue=True,
    )

    # 5. Exception/error log (captures ERROR and CRITICAL only, with full
    #    stack traces and variable diagnostics)
    logger.add(
        os.path.join(error_dir, f"error_{{time:YYYY-MM-DD}}_{os.getpid()}.log"),
        level="ERROR",
        rotation="50 MB",
        retention="60 days",
        compression="zip",
        encoding="utf-8",
        enqueue=True,
        backtrace=True,
        diagnose=True,
    )
