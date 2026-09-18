"""Service layer for the session todo list.

Owns two behaviors the store does not:

* **Default-downgrade** — an unknown ``category`` collapses to ``quick`` and
  an unknown ``delegation`` collapses to ``self`` before persistence.
* **Transition barrier** — a todo may only be marked ``completed`` once the
  work it points at is actually finished: a linked subagent run must no longer
  be live, and a linked TaskFlow step must be ``done``. Plain todos (no links)
  are never blocked.

The barrier reaches three external seams (subagent registry liveness, TaskFlow
step status, WS push). Each is a **module-level injectable reference** — the
same pattern as ``agent/tools/taskflow/tools/_dispatch.py`` — so tests can
substitute it without touching the real runtime. The store is reached through
the ``store_sqlite`` module object for the same reason.

The WS push is fail-open: no registered websocket, or a failed ``send_text``,
is logged and swallowed; it never rolls back or masks a successful persistence.
"""

import json

from loguru import logger

from runtime import relation_register

from .config import (
    DEFAULT_CATEGORY,
    DEFAULT_DELEGATION,
    VALID_CATEGORIES,
    VALID_DELEGATIONS,
)
from .registry import store_sqlite as store

# TaskFlow's terminal step status; a linked step must reach it before the todo
# may complete. Imported lazily by _step_status to keep this module importable
# without the taskflow package.
_TASKFLOW_STEP_DONE = "done"

# Session state key holding the plan file pointer (session-scoped
# ``workspace/sessions/<id>/plans/*.md``; legacy ``.omo/plans/*.md`` accepted)
# that the prompt builder reads to locate the plan's knowledge summary.
_PLAN_REF_STATE_KEY = "plan_ref"


class TodoStoreError(Exception):
    """Raised when a todo mutation is rejected (transition barrier)."""


def _get_run_by_child_session_key(child_session_key: str):
    """Injectable seam: look up a subagent run record by child session key."""
    from agent.tools.subagent.registry import get_run_by_child_session_key

    return get_run_by_child_session_key(child_session_key)


def _is_live_unended_run(run) -> bool:
    """Injectable seam: True while the run is RUNNING or INTERRUPTED."""
    from agent.tools.subagent.registry import is_live_unended_run

    return is_live_unended_run(run)


async def _load_flow(flow_id: str, session_id: str) -> dict | None:
    """Injectable seam: read-only TaskFlow flow lookup (never schedules).

    Reads are session-scoped: a flow owned by another session reads as missing.
    """
    from agent.tools.taskflow.registry import get_flow

    return await get_flow(flow_id, session_id)


def _step_status(step: dict) -> str:
    """Injectable seam: derive a TaskFlow step's status (legacy-aware)."""
    from agent.tools.taskflow.tools._shared import step_status

    return step_status(step)


def _validate_todos(todos: list[dict]) -> list[dict]:
    """Return shallow copies with the category/delegation vocabularies default-downgraded.

    Status and priority are left exactly as given. Copies (not in-place edits)
    keep the caller's dicts untouched.
    """
    validated: list[dict] = []
    for todo in todos:
        item = dict(todo)
        if item.get("category") not in VALID_CATEGORIES:
            item["category"] = DEFAULT_CATEGORY
        if item.get("delegation") not in VALID_DELEGATIONS:
            item["delegation"] = DEFAULT_DELEGATION
        validated.append(item)
    return validated


def _is_subagent_running(child_session_key: str) -> bool:
    """True when a run record exists for the key and is still live.

    A missing record is not a blocker: the subagent either never registered or
    has already been swept, and the todo must remain completable.
    """
    run = _get_run_by_child_session_key(child_session_key)
    if run is None:
        return False
    return _is_live_unended_run(run)


async def _read_taskflow_step_status(
    flow_id: str, step_id: str, session_id: str
) -> tuple[str | None, str | None]:
    """Read a linked TaskFlow step's status, or the reason it is unresolvable.

    Returns ``(status, problem)`` with exactly one meaningful value. ``problem``
    is a human-readable reason when the flow is absent (or owned by another
    session) or the step is not in it; both are treated as "not done" by the
    barrier.
    """
    flow = await _load_flow(flow_id, session_id)
    if flow is None:
        return None, f"TaskFlow flow '{flow_id}' not found"
    steps = (flow.get("state") or {}).get("steps") or []
    for step in steps:
        if step.get("step_id") == step_id:
            return _step_status(step), None
    return None, f"TaskFlow step '{step_id}' not found in flow '{flow_id}'"


async def _assert_transition_allowed(todo: dict, session_id: str) -> None:
    """Raise :class:`TodoStoreError` unless a ``completed`` todo may complete.

    Two independent sources gate the transition:

    1. a linked subagent run that is still live;
    2. a linked TaskFlow step whose status is not ``done`` (or that cannot be
       resolved at all: missing flow / missing step). The flow read is scoped
       to ``session_id``, so another session's flow never satisfies the gate.

    Plain todos carry neither link and are therefore never blocked.
    """
    subagent_id = todo.get("subagent_id")
    if subagent_id and _is_subagent_running(str(subagent_id)):
        raise TodoStoreError(
            f"Cannot mark todo completed: subagent {subagent_id} is still running. "
            "Wait for it to finish first."
        )

    flow_id = todo.get("flow_id")
    step_id = todo.get("step_id")
    if flow_id and step_id:
        status, problem = await _read_taskflow_step_status(str(flow_id), str(step_id), session_id)
        if problem is not None:
            raise TodoStoreError(
                f"Cannot mark todo completed: {problem}. Call taskflow_wait_all then "
                "taskflow_resume to inject the result before marking completed."
            )
        if status != _TASKFLOW_STEP_DONE:
            raise TodoStoreError(
                f"Cannot mark todo completed: TaskFlow step {step_id} "
                f"(flow {flow_id}) is '{status}'. Call taskflow_wait_all then "
                "taskflow_resume to inject the result before marking completed."
            )


async def _push_todo_update(session_id: str, todos: list[dict]) -> None:
    """Best-effort ``todo_updated`` WS frame; never raises.

    Mirrors ``server/service/heartbeat.py``: an absent websocket or a failed
    send is logged and swallowed so it cannot break the persistence path.
    """
    try:
        websocket = relation_register.get_websocket_by_session_id(session_id)
        if websocket is None:
            return
        payload = {
            "event": "todo_updated",
            "session_id": session_id,
            "content": {"todos": todos},
        }
        await websocket.send_text(json.dumps(payload, ensure_ascii=False))
    except Exception as e:
        logger.warning("Failed to push todo_updated for session {}: {}", session_id, e)


class TodoService:
    """Session-scoped todo mutations: validate, barrier, persist, push, return."""

    @staticmethod
    async def update_todos(
        session_id: str,
        todos: list[dict],
        plan_ref: str | None = None,
    ) -> list[dict]:
        """Full-replace a session's todos, enforcing the default-downgrade
        and the transition barrier.

        A non-empty ``plan_ref`` is remembered once per session under the
        ``plan_ref`` state key; the prompt builder reads it to locate the
        plan's knowledge summary. The barrier runs BEFORE any write, so a
        rejected transition leaves the persisted rows untouched. Returns the
        freshly read-back list.
        """
        validated = _validate_todos(todos)
        for todo in validated:
            if todo.get("status") == "completed":
                await _assert_transition_allowed(todo, session_id)
        await store.replace_all(session_id, validated)
        if plan_ref:
            from runtime import state_register_db

            state_register_db.set_state(session_id, _PLAN_REF_STATE_KEY, plan_ref)
        latest = await store.get_todos(session_id)
        await _push_todo_update(session_id, latest)
        return latest

    @staticmethod
    async def get_todos(session_id: str) -> list[dict]:
        """Read the session's todos from the store."""
        return await store.get_todos(session_id)


__all__ = [
    "TodoService",
    "TodoStoreError",
    "_assert_transition_allowed",
    "_push_todo_update",
    "_read_taskflow_step_status",
    "_validate_todos",
]
