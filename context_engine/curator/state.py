import json
from typing import Any
from loguru import logger
from context_engine.curator.constants import CURATOR_STATE_FILE
from context_engine.curator.helpers import _ensure_dir, _atomic_json_write


def _default_state() -> dict[str, Any]:
    return {
        "last_run_at": None,
        "last_run_duration_seconds": None,
        "last_run_summary": None,
        "last_run_summary_shown_at": None,
        "last_report_path": None,
        "paused": False,
        "run_count": 0,
        # UI-configurable auto-maintenance interval (days, 1..5). None = fall
        # back to `curator.yaml`'s `interval_hours` (default 5 days).
        "auto_interval_days": None,
        # ISO timestamp of the last maintenance run (manual or auto). Mirrors
        # `last_run_at` but is surfaced to the client as "last maintenance time".
        "last_maintenance_at": None,
    }


def load_state() -> dict[str, Any]:
    if not CURATOR_STATE_FILE.exists():
        return _default_state()
    try:
        data = json.loads(CURATOR_STATE_FILE.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            base = _default_state()
            base.update({k: v for k, v in data.items() if k in base or k.startswith("_")})
            return base
    except (OSError, json.JSONDecodeError) as e:
        logger.debug("Failed to read curator state: {}", e)
    return _default_state()


def save_state(data: dict[str, Any]) -> None:
    try:
        _ensure_dir(CURATOR_STATE_FILE.parent)
        _atomic_json_write(CURATOR_STATE_FILE, data)
    except Exception as e:
        logger.debug("Failed to save curator state: {}", e)


def is_paused() -> bool:
    return bool(load_state().get("paused"))
