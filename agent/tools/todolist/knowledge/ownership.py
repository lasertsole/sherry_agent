"""Plan-ownership checks for the ``knowledge`` tool (plan-level isolation).

The knowledge store is keyed by plan name, so isolation cannot be a plain
session column: one plan may legitimately be worked on by several sessions. A
session is associated with a plan when any of three sources links them:

1. the session's ``plan_ref`` state key (``state_register_db``);
2. a ``plan_ref`` on one of this session's todos (``todos.db``, filtered by
   ``session_id`` in SQL);
3. a ``.omo/boulder.json`` work whose ``plan_name`` matches and whose
   ``session_ids`` list contains this session (multi-session collaboration).

Compatibility: the plan-extraction nudge falls back to a session-derived plan
name (``session-<id8>``, ``summarization/nudges.py::_build_plan_context``) when
no plan file resolves. That name is generated from this session id, so it
belongs to this session and stays writable.

Every read is fail-open: a missing/corrupt boulder file, a store error or an
empty ``session_id`` yields an empty association set (deny), never an
exception.
"""

import json
from collections.abc import Iterator
from pathlib import Path

from loguru import logger

from agent.tools.todolist.registry.store_sqlite import get_todos_sync
from config.path import resolve_boulder_path
from runtime import state_register_db

_PLAN_REF_STATE_KEY = "plan_ref"
_SESSION_FALLBACK_PREFIX = "session-"


def _plan_name_of(ref: object) -> str:
    """Map a plan reference (path or bare name) to its plan-name stem."""
    if isinstance(ref, str) and ref.strip():
        return Path(ref.strip()).stem
    return ""


def _state_plan_names(session_id: str) -> set[str]:
    """Plan names from this session's ``plan_ref`` state key."""
    try:
        value = state_register_db.get_state(session_id, _PLAN_REF_STATE_KEY, "")
    except Exception:
        logger.debug(
            "knowledge ownership: state plan_ref read failed for {}", session_id, exc_info=True
        )
        return set()
    name = _plan_name_of(value)
    return {name} if name else set()


def _todo_plan_names(session_id: str) -> set[str]:
    """Plan names from this session's todos (store filters by session in SQL)."""
    try:
        todos = get_todos_sync(session_id)
    except Exception:
        logger.debug("knowledge ownership: todos read failed for {}", session_id, exc_info=True)
        return set()
    names = {_plan_name_of(todo.get("plan_ref")) for todo in todos}
    names.discard("")
    return names


def _iter_boulder_works(boulder: object) -> Iterator[object]:
    """Yield works from a boulder document (dict-keyed or list-shaped)."""
    if not isinstance(boulder, dict):
        return
    works = boulder.get("works")
    if isinstance(works, dict):
        yield from works.values()
    elif isinstance(works, list):
        yield from works


def _boulder_plan_names(session_id: str) -> set[str]:
    """Plan names of boulder works whose ``session_ids`` include this session."""
    boulder_path = resolve_boulder_path()
    try:
        if not boulder_path.is_file():
            return set()
        boulder = json.loads(boulder_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        logger.debug("knowledge ownership: boulder read failed at {}", boulder_path, exc_info=True)
        return set()

    names: set[str] = set()
    for work in _iter_boulder_works(boulder):
        if not isinstance(work, dict):
            continue
        plan_name = work.get("plan_name")
        session_ids = work.get("session_ids")
        if not isinstance(plan_name, str) or not plan_name.strip():
            continue
        if isinstance(session_ids, list) and session_id in session_ids:
            names.add(plan_name.strip())
    return names


def associated_plan_names(session_id: str) -> set[str]:
    """Every plan name this session may read / write / list."""
    sid = session_id.strip() if isinstance(session_id, str) else ""
    if not sid:
        logger.debug("knowledge ownership: empty session_id; no plan association")
        return set()
    names = _state_plan_names(sid) | _todo_plan_names(sid) | _boulder_plan_names(sid)
    # Session-derived fallback name used when no plan file resolves: it can only
    # have been generated from this session id, so it is inherently its own.
    names.add(f"{_SESSION_FALLBACK_PREFIX}{sid[:8]}")
    return names


def is_plan_associated(session_id: str, plan_name: str) -> bool:
    """True when this session may access knowledge for *plan_name*."""
    if not isinstance(plan_name, str) or not plan_name.strip():
        return False
    return plan_name.strip() in associated_plan_names(session_id)


__all__ = ["associated_plan_names", "is_plan_associated"]
