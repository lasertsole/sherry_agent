"""Plan-mode session state helpers (shared by the tools + the read sides).

Writes go into BOTH register tiers: the in-memory register so the hot-path
readers (``ToolSelectionMiddleware``) see a status change immediately, and the
SQLite register so plan mode survives a restart. Reads prefer the memory tier
and fall back to the DB (a cold memory tier right after a restart). Every
accessor is fail-open — a broken register must never break a turn.
"""

from __future__ import annotations

from loguru import logger

from config.features import PLAN_MODE, PLAN_ACTIVE_STATUSES  # noqa: F401  (re-exported)
from runtime import StateKey, state_register_db, state_register_mem

__all__ = [
    "PLAN_ACTIVE_STATUSES",
    "PLAN_MODE",
    "read_plan_status",
    "set_plan_state",
]


def read_plan_status(session_id: str) -> str:
    """The session's plan status (``""`` when unset) — mem first, DB fallback."""
    if not session_id:
        return ""
    try:
        value = state_register_mem.get_state(session_id, StateKey.PLAN_STATUS, None)
        if isinstance(value, str) and value:
            return value
    except Exception:  # noqa: BLE001 - fail-open read
        logger.debug("plan_mode: mem status read failed", exc_info=True)
    try:
        value = state_register_db.get_state(session_id, StateKey.PLAN_STATUS, None)
    except Exception:  # noqa: BLE001 - fail-open read
        logger.debug("plan_mode: db status read failed", exc_info=True)
        return ""
    return value if isinstance(value, str) else ""


def read_plan_field(session_id: str, key: StateKey, default: object = None) -> object:
    """One plan field (mem first, DB fallback) — fail-open."""
    if not session_id:
        return default
    try:
        value = state_register_mem.get_state(session_id, key, None)
        if value is not None:
            return value
    except Exception:  # noqa: BLE001 - fail-open read
        logger.debug("plan_mode: mem read failed for {}", key, exc_info=True)
    try:
        value = state_register_db.get_state(session_id, key, default)
    except Exception:  # noqa: BLE001 - fail-open read
        logger.debug("plan_mode: db read failed for {}", key, exc_info=True)
        return default
    return value if value is not None else default


def set_plan_state(
    session_id: str,
    *,
    status: str | None = None,
    revision: str | None = None,
    file_path: str | None = None,
    mutation_count: int | None = None,
) -> None:
    """Write the given plan fields into both register tiers (fail-open)."""
    if not session_id:
        return
    fields: list[tuple[StateKey, object]] = []
    if status is not None:
        fields.append((StateKey.PLAN_STATUS, status))
    if revision is not None:
        fields.append((StateKey.PLAN_REVISION, revision))
    if file_path is not None:
        fields.append((StateKey.PLAN_FILE_PATH, file_path))
    if mutation_count is not None:
        fields.append((StateKey.PLAN_MUTATION_COUNT, int(mutation_count)))
    for register in (state_register_mem, state_register_db):
        for key, value in fields:
            try:
                register.set_state(session_id, key, value)
            except Exception:  # noqa: BLE001 - never break a tool over state
                logger.debug("plan_mode: {} write failed for {}", register, key, exc_info=True)
