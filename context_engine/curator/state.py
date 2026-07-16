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


def set_paused(paused: bool) -> None:
    state = load_state()
    state["paused"] = bool(paused)
    save_state(state)


def is_paused() -> bool:
    return bool(load_state().get("paused"))
