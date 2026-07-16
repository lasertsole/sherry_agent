import os
from typing import Any
from loguru import logger
from dotenv import load_dotenv

from config import ROOT_DIR, ENV_PATH
from context_engine.curator.constants import (
    DEFAULT_INTERVAL_HOURS,
    DEFAULT_MIN_IDLE_HOURS,
    DEFAULT_STALE_AFTER_DAYS,
    DEFAULT_ARCHIVE_AFTER_DAYS,
    DEFAULT_CONSOLIDATE,
)

load_dotenv(ENV_PATH, override=True)


def _load_config() -> dict[str, Any]:
    try:
        import yaml
        cfg_path = ROOT_DIR / "curator.yaml"
        if cfg_path.exists():
            with open(cfg_path, "r", encoding="utf-8") as f:
                cfg = yaml.safe_load(f) or {}
            if isinstance(cfg, dict):
                return cfg
    except Exception as e:
        logger.debug("Failed to load curator config: {}", e)
    return {}


def is_enabled() -> bool:
    return bool(_load_config().get("enabled", True))


def get_interval_hours() -> int:
    try:
        return int(_load_config().get("interval_hours", DEFAULT_INTERVAL_HOURS))
    except (TypeError, ValueError):
        return DEFAULT_INTERVAL_HOURS


def get_min_idle_hours() -> float:
    try:
        return float(_load_config().get("min_idle_hours", DEFAULT_MIN_IDLE_HOURS))
    except (TypeError, ValueError):
        return DEFAULT_MIN_IDLE_HOURS


def get_stale_after_days() -> int:
    try:
        return int(_load_config().get("stale_after_days", DEFAULT_STALE_AFTER_DAYS))
    except (TypeError, ValueError):
        return DEFAULT_STALE_AFTER_DAYS


def get_archive_after_days() -> int:
    try:
        return int(_load_config().get("archive_after_days", DEFAULT_ARCHIVE_AFTER_DAYS))
    except (TypeError, ValueError):
        return DEFAULT_ARCHIVE_AFTER_DAYS


def get_consolidate() -> bool:
    return bool(_load_config().get("consolidate", DEFAULT_CONSOLIDATE))


def get_prune_builtins() -> bool:
    return bool(_load_config().get("prune_builtins", True))
