import os
import sys
import time
from pathlib import Path
from loguru import logger
from dotenv import load_dotenv
from config import ROOT_DIR, ENV_PATH

load_dotenv(ENV_PATH, override = True)

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
    """从 .env 读取 LOG_LEVEL，无效值回退 INFO。"""
    raw = os.getenv("LOG_LEVEL", "INFO").strip().upper()
    valid = {"TRACE", "DEBUG", "INFO", "SUCCESS", "WARNING", "ERROR", "CRITICAL"}
    return raw if raw in valid else "INFO"


def init_logger(log_dir=ROOT_DIR / "logs/output", timeout_days: int = 7):
    """初始化全局日志配置"""
    # 按类型创建独立的日志子目录：info/、error/ 与全部记录的 all/
    log_dir = Path(log_dir)
    info_dir = log_dir / "info"
    error_dir = log_dir / "error"
    all_dir = log_dir / "all"
    for d in (log_dir, info_dir, error_dir, all_dir):
        if not os.path.exists(d):
            os.makedirs(d)

    # 0. 删除过期日志（递归遍历各类型子目录）
    _clean_expired_logs(log_dir, timeout_days)

    level = _resolve_level()

    # 1. 清除 Loguru 默认配置
    logger.remove()

    # 2. 控制台输出
    logger.add(
        sys.stderr,
        level=level,
        format="<green>{time:YYYY-MM-DD HH:mm:ss.SSS}</green> | <level>{level: <8}</level> | <cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> - <level>{message}</level>",
        enqueue=True
    )

    # 3. 正常流水日志（只捕获 INFO 级别，供日常查看；WARNING/ERROR/CRITICAL
    #    归 error 侧排查，TRACE/DEBUG 归全量日志）
    logger.add(
        os.path.join(info_dir, f"info_{{time:YYYY-MM-DD}}_{os.getpid()}.log"),
        level="INFO",
        filter=lambda record: record["level"].name == "INFO",
        rotation="100 MB",
        retention="30 days",
        compression="zip",
        encoding="utf-8",
        enqueue=True
    )

    # 4. 全量日志（记录所有级别，包括 TRACE/DEBUG/WARNING，供全面排查）
    logger.add(
        os.path.join(all_dir, f"all_{{time:YYYY-MM-DD}}_{os.getpid()}.log"),
        level="TRACE",
        rotation="100 MB",
        retention="30 days",
        compression="zip",
        encoding="utf-8",
        enqueue=True
    )

    # 5. 异常错误日志（只捕获 ERROR 和 CRITICAL，带有完整的堆栈和变量诊断）
    logger.add(
        os.path.join(error_dir, f"error_{{time:YYYY-MM-DD}}_{os.getpid()}.log"),
        level="ERROR",
        rotation="50 MB",
        retention="60 days",
        compression="zip",
        encoding="utf-8",
        enqueue=True,
        backtrace=True,
        diagnose=True
    )