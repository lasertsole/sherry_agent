"""Loguru logging configuration (console + rotating file sinks)."""

import os
import sys
import threading
import time
from pathlib import Path
from loguru import logger
from config import LOG_DIR
from config.features import LOGS
from config.path import load_env
from config.sherry_settings import get_sherry_setting

# .env plus the network.jsonc overlay (log level / sinks can come from either)
load_env()

#: Files modified more recently than this are NEVER deleted by the size sweep:
#: they are being written right now (this process's own sinks, or another live
#: process's), and unlinking an open file frees no space until close anyway.
_SIZE_SWEEP_QUIESCENCE_S = 300

_LOG_EXTENSIONS = (".log", ".zip")


def _log_files(log_dir: Path) -> list[Path]:
    """Every retained log artifact under ``log_dir`` (all per-type folders)."""
    if not log_dir.is_dir():
        return []
    return [p for p in log_dir.rglob("*") if p.is_file() and p.suffix in _LOG_EXTENSIONS]


def _clean_expired_logs(log_dir: Path, timeout_days: int | None = None):
    """Recursively delete log files older than the retention window.

    Walks ``log_dir`` including type sub-directories (``info/``, ``error/``)
    so archived logs in every per-type folder are pruned. ``timeout_days``
    overrides the ``LOGS["retention_days"]`` default (0 disables the rule).
    """
    days = LOGS["retention_days"] if timeout_days is None else timeout_days
    if days <= 0:
        return
    cutoff = time.time() - days * 86400
    for p in _log_files(log_dir):
        try:
            if p.stat().st_mtime < cutoff:
                p.unlink(missing_ok=True)
        except OSError:
            continue


def _enforce_total_size(log_dir: Path, max_total_mb: int | None = None) -> int:
    """Delete the OLDEST log files until the tree fits ``max_total_mb``.

    Returns the number of files removed. Files still being written (mtime
    inside the quiescence window) are skipped, so a burst that lives entirely
    inside fresh files simply waits for the next sweep.
    """
    cap_mb = LOGS["max_total_mb"] if max_total_mb is None else max_total_mb
    if cap_mb <= 0:
        return 0
    files = _log_files(log_dir)
    total = 0
    entries: list[tuple[float, Path, int]] = []
    now = time.time()
    for p in files:
        try:
            stat = p.stat()
        except OSError:
            continue
        total += stat.st_size
        entries.append((stat.st_mtime, p, stat.st_size))
    if total <= cap_mb * 1024 * 1024:
        return 0
    removed = 0
    for mtime, p, _size in sorted(entries):
        if total <= cap_mb * 1024 * 1024:
            break
        if now - mtime < _SIZE_SWEEP_QUIESCENCE_S:
            continue
        try:
            p.unlink(missing_ok=True)
            removed += 1
            total -= _size
        except OSError:
            continue
    if removed:
        logger.warning("log retention: tree over {} MiB; removed {} old file(s)", cap_mb, removed)
    return removed


def _sweep_forever(log_dir: Path, interval_s: int) -> None:
    """The background retention loop (sleep-first: boot already swept)."""
    while True:
        time.sleep(interval_s)
        try:
            _clean_expired_logs(log_dir)
            _enforce_total_size(log_dir)
        except Exception:  # noqa: BLE001 — a sweep failure must never surface
            logger.warning("log retention sweep failed", exc_info=True)


def _start_retention_sweeper(log_dir: Path) -> None:
    """Start the periodic sweep daemon thread (a no-op when disabled)."""
    interval = int(LOGS["sweep_interval_s"])
    if interval <= 0:
        return
    thread = threading.Thread(
        target=_sweep_forever, args=(log_dir, interval), name="log-retention", daemon=True
    )
    thread.start()


def _resolve_level() -> str:
    """Read LOG_LEVEL from sherry.jsonc, fall back to INFO on invalid values."""
    raw = str(get_sherry_setting("LOG_LEVEL")).strip().upper()
    valid = {"TRACE", "DEBUG", "INFO", "SUCCESS", "WARNING", "ERROR", "CRITICAL"}
    return raw if raw in valid else "INFO"


def init_logger(log_dir=LOG_DIR, timeout_days: int = 7):
    """Initialize the global logging configuration."""
    # Create per-type log sub-directories: info/, error/, all/ (everything) and
    # json/ (machine-readable JSONL)
    log_dir = Path(log_dir)
    info_dir = log_dir / "info"
    error_dir = log_dir / "error"
    all_dir = log_dir / "all"
    json_dir = log_dir / "json"
    for d in (log_dir, info_dir, error_dir, all_dir, json_dir):
        if not os.path.exists(d):
            os.makedirs(d)

    # 0. Delete expired logs (walk each per-type sub-directory recursively),
    #    then enforce the total-size cap, then start the periodic sweeper so a
    #    long-running process keeps pruning (boot-only sweeps never fired again).
    _clean_expired_logs(log_dir, timeout_days)
    _enforce_total_size(log_dir)
    _start_retention_sweeper(log_dir)

    level = _resolve_level()

    # 1. Clear Loguru's default configuration
    logger.remove()

    # 1a. Redact secrets BEFORE any sink formats or writes a record. Installed
    #     here (the single place sinks are configured) so the guarantee holds for
    #     every sink added below and any added later: a log file outlives the
    #     session and is read by people who never saw the credential.
    from pub.security.redact_formatter import install_log_redaction

    install_log_redaction()

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

    # 4b. Machine-readable JSONL twin of the full log: one JSON object per line
    #     with ``text`` (the message) and ``record.extra`` (the fields a
    #     ``logger.bind``/``logger.contextualize`` scope attached — turn_id,
    #     session_id, middleware, hook, …). The text sinks above stay the
    #     human-readable channel; this one exists so a whole turn can be pulled
    #     out with a single ``jq 'select(.record.extra.turn_id == "…")'``.
    #     The redaction patcher runs before every sink, this one included.
    logger.add(
        os.path.join(json_dir, f"json_{{time:YYYY-MM-DD}}_{os.getpid()}.log"),
        level="TRACE",
        rotation="100 MB",
        retention="30 days",
        compression="zip",
        encoding="utf-8",
        enqueue=True,
        serialize=True,
    )

    # 5. Exception/error log (captures ERROR and CRITICAL only, with the failing
    #    source line of every frame — no `diagnose` variable dump: frame values
    #    are printed verbatim and hold identifiers no redaction family knows)
    logger.add(
        os.path.join(error_dir, f"error_{{time:YYYY-MM-DD}}_{os.getpid()}.log"),
        level="ERROR",
        rotation="50 MB",
        retention="60 days",
        compression="zip",
        encoding="utf-8",
        enqueue=True,
        backtrace=True,
        diagnose=False,
    )
