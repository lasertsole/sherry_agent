"""Active-plan detection for the summarization prompt (plan-active window).

Plan-scoped lessons (``SummaryDoc.active_plan_notes``) live only as long as the
plan is being executed. This module answers "is a plan active for this
session?" by reusing the two raw association sources of
``agent/tools/todolist/knowledge/ownership.py``:

1. the session's ``plan_ref`` state key (``state_register_db``);
2. a ``plan_ref`` on one of this session's todos (``todos.db``, SQL-filtered by
   ``session_id``).

The boulder source of ``ownership.association_plan_refs`` is deliberately
excluded: the summary-level notes belong to the plan this session is executing.

Completion gate: when the session has todos and every one of them is
``completed`` / ``cancelled``, the plan is finished — the resolver returns
``None``, so the renderer drops the notes section and the next document clears
the array. A session with a ``plan_ref`` but no todos yet is still active.

Every read is fail-open: a broken state/todo store yields no plan, never an
exception. The renderer is pure and deterministic (same plan -> same bytes).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from loguru import logger

from config.path import ROOT_DIR, resolve_plan_path
from runtime import StateKey, state_register_db

_PLAN_REF_STATE_KEY = StateKey.PLAN_REF
_DONE_STATUSES = frozenset({"completed", "cancelled"})

# Render bounds: the block is a pointer, not the plan itself. The first N open
# todos are shown verbatim (bounded per item); the rest collapse into a count.
_MAX_OPEN_ITEMS = 8
_MAX_TODO_CHARS = 120


@dataclass(frozen=True, slots=True)
class ActivePlan:
    """The plan this session is currently executing (summary-prompt view)."""

    plan_ref: str
    plan_name: str
    display_path: str
    open_items: tuple[str, ...]
    done_count: int
    total_count: int


def _read_todos(session_id: str) -> list[dict]:
    """Sync todo read (lazy import keeps middleware import light); [] on failure."""
    try:
        from agent.tools.todolist.registry.store_sqlite import get_todos_sync

        return get_todos_sync(session_id)
    except Exception:
        logger.debug("plan context: todos read failed for {}", session_id, exc_info=True)
        return []


def _resolve_plan_ref(session_id: str, todos: Sequence[dict]) -> str:
    """Session-level state first, then the first todo carrying a ``plan_ref``."""
    try:
        stored = state_register_db.get_state(session_id, _PLAN_REF_STATE_KEY, "")
    except Exception:
        logger.debug("plan context: state plan_ref read failed for {}", session_id, exc_info=True)
        stored = ""
    if isinstance(stored, str) and stored.strip():
        return stored.strip()
    for todo in todos:
        ref = todo.get("plan_ref")
        if isinstance(ref, str) and ref.strip():
            return ref.strip()
    return ""


def _todo_text(todo: dict) -> str:
    content = todo.get("content")
    text = content.strip() if isinstance(content, str) else str(content or "").strip()
    if len(text) > _MAX_TODO_CHARS:
        return text[:_MAX_TODO_CHARS] + "…"
    return text


def _display_path(resolved: Path | None, ref: str) -> str:
    """Repo-relative posix path when the file resolves; the raw ref otherwise."""
    if resolved is None:
        return ref
    try:
        return resolved.relative_to(ROOT_DIR).as_posix()
    except ValueError:
        return resolved.as_posix()


def resolve_active_plan(session_id: str) -> ActivePlan | None:
    """The active plan for *session_id*, or ``None``.

    ``None`` means either no state/todo ``plan_ref`` association, or the
    session's todos are all ``completed`` / ``cancelled`` (plan finished) — in
    both cases the summary drops ``active_plan_notes``. Never raises.
    """
    sid = session_id.strip() if isinstance(session_id, str) else ""
    if not sid:
        return None

    todos = _read_todos(sid)
    ref = _resolve_plan_ref(sid, todos)
    if not ref:
        return None

    if todos and all(todo.get("status") in _DONE_STATUSES for todo in todos):
        return None

    open_items = tuple(
        text
        for text in (_todo_text(todo) for todo in todos if todo.get("status") not in _DONE_STATUSES)
        if text
    )
    done_count = sum(1 for todo in todos if todo.get("status") in _DONE_STATUSES)

    resolved: Path | None = None
    try:
        resolved = resolve_plan_path(ref, sid)
    except Exception:
        logger.debug("plan context: path resolution failed for {}", ref, exc_info=True)
    plan_name = resolved.stem if resolved is not None else Path(ref).stem
    if not plan_name:
        plan_name = ref

    return ActivePlan(
        plan_ref=ref,
        plan_name=plan_name,
        display_path=_display_path(resolved, ref),
        open_items=open_items,
        done_count=done_count,
        total_count=len(todos),
    )


def render_plan_context(plan: ActivePlan) -> str:
    """Render the prompt block: relative path + plan name + open todo summary.

    The plan body is never injected — the model can ``read_file`` the path.
    Deterministic for a given :class:`ActivePlan`.
    """
    lines = [
        "## Active Plan (authoritative)",
        f"- Plan: {plan.plan_name}",
        f"- File: {plan.display_path}",
        f"- Todo progress: {plan.done_count}/{plan.total_count} done",
    ]
    if plan.open_items:
        lines.append("- Open todos:")
        for index, item in enumerate(plan.open_items[:_MAX_OPEN_ITEMS], start=1):
            lines.append(f"  {index}. {item}")
        omitted = len(plan.open_items) - _MAX_OPEN_ITEMS
        if omitted > 0:
            lines.append(f"  (+{omitted} more)")
    else:
        lines.append("- Open todos: (none)")
    return "\n".join(lines)


__all__ = ["ActivePlan", "render_plan_context", "resolve_active_plan"]
