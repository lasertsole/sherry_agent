import os
import sys
import time
from pathlib import Path
from loguru import logger
from config import ROOT_DIR


def _clean_expired_logs(log_dir: Path, timeout_days: int = 7):
    """Delete log files older than `days` in the log directory."""
    cutoff = time.time() - timeout_days * 86400
    if not log_dir.is_dir():
        return
    for p in log_dir.iterdir():
        if p.is_file() and p.suffix in (".log", ".zip") and p.stat().st_mtime < cutoff:
            p.unlink(missing_ok=True)


def init_logger(log_dir=ROOT_DIR / "logs/output", timeout_days: int = 7):
    """初始化全局日志配置"""
    if not os.path.exists(log_dir):
        os.makedirs(log_dir)

    # 0. 删除过期日志
    _clean_expired_logs(Path(log_dir), timeout_days)

    # 1. 清除 Loguru 默认配置
    logger.remove()

    # 2. 控制台输出（包含所有 INFO 及以上日志）
    logger.add(
        sys.stderr,
        level="INFO",
        format="<green>{time:YYYY-MM-DD HH:mm:ss.SSS}</green> | <level>{level: <8}</level> | <cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> - <level>{message}</level>",
        enqueue=True
    )

    # 3. 正常流水日志（引入 {os.getpid()} 规避多进程切分死锁，利用 filter 只保留 INFO 和 SUCCESS）
    logger.add(
        os.path.join(log_dir, f"info_{{time:YYYY-MM-DD}}_{os.getpid()}.log"),
        level="INFO",
        filter=lambda record: record["level"].name in ("INFO", "SUCCESS"),
        rotation="100 MB",
        retention="30 days",
        compression="zip",
        encoding="utf-8",
        enqueue=True
    )

    # 4. 异常错误日志（只捕获 ERROR 和 CRITICAL，带有完整的堆栈和变量诊断）
    logger.add(
        os.path.join(log_dir, f"error_{{time:YYYY-MM-DD}}_{os.getpid()}.log"),
        level="ERROR",
        rotation="50 MB",
        retention="60 days",
        compression="zip",
        encoding="utf-8",
        enqueue=True,
        backtrace=True,
        diagnose=True
    )