"""Service layer for the session todo list.

Owns three behaviors the store does not:

* **Default-downgrade** — an unknown ``category`` collapses to ``quick`` and
  an unknown ``delegation`` collapses to ``self`` before persistence.
* **Transition barrier** — a todo may only be marked ``completed`` once the
  work it points at is actually finished: a linked subagent run must no longer
  be live, and a linked TaskFlow step must be ``done``. Plain todos (no links)
  are never blocked.
* **Local action semantics** — patch/remove/reattach/add resolve a stable
  ``todo_id``, preserve reference fields, and turn a stale id into
  ``TodoIdNotFoundError`` carrying the current valid list. Removing a flow's
  explicit root triggers an immediate compensation attempt; the sweep is still
  the backstop.

The barrier reaches three external seams (subagent registry liveness, TaskFlow
step status, WS push). Each is a **module-level injectable reference** — the
same pattern as ``agent/tools/taskflow/tools/_dispatch.py`` — so tests can
substitute it without touching the real runtime. The store is reached through
the ``store_sqlite`` module object for the same reason.

The WS push is fail-open: no registered websocket, or a failed ``send_text``,
is logged and swallowed; it never rolls back or masks a successful persistence.
"""

import asyncio

from loguru import logger

from runtime import StateKey
from runtime.session.ws_push import broadcast_to_session

from .config import (
    DEFAULT_CATEGORY,
    DEFAULT_DELEGATION,
    DEFAULT_PRIORITY,
    VALID_CATEGORIES,
    VALID_DELEGATIONS,
)
from .registry import store_sqlite as store

# TaskFlow's terminal step status; a linked step must reach it before the todo
# may complete. Imported lazily by _step_status to keep this module importable
# without the taskflow package.
_TASKFLOW_STEP_DONE = "done"

# TaskFlow terminal statuses: a FLOW-level todo (flow_id with no step_id) is a
# GC root for the whole flow, so it may only be completed once the flow itself
# is terminal. A step-level link keeps the existing per-step barrier.
_TASKFLOW_TERMINAL = frozenset({"done", "failed", "cancelled"})

# Session state key holding the plan file pointer (session-scoped
# ``workspace/sessions/<id>/plans/*.md``) that the prompt builder reads to
# locate the plan's knowledge summary.
_PLAN_REF_STATE_KEY = StateKey.PLAN_REF


class TodoStoreError(Exception):
    """Raised when a todo mutation is rejected (transition barrier)."""


class TodoIdNotFoundError(Exception):
    """A local todo operation named an id that is not in this session.

    Carries the current valid list so the tool layer can tell the model exactly
    which ids exist instead of returning an opaque failure.
    """

    def __init__(self, todo_id: int, valid_todos: list[dict], *, field: str = "todo_id") -> None:
        self.todo_id = todo_id
        self.valid_todos = valid_todos
        self.field = field
        super().__init__(f"{field} {todo_id} not found")


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
                f"Cannot mark todo completed: {problem}. Call taskflow_step(action='wait_all') then "
                "taskflow_step(action='resume') to inject the result before marking completed."
            )
        if status != _TASKFLOW_STEP_DONE:
            raise TodoStoreError(
                f"Cannot mark todo completed: TaskFlow step {step_id} "
                f"(flow {flow_id}) is '{status}'. Call taskflow_step(action='wait_all') then "
                "taskflow_step(action='resume') to inject the result before marking completed."
            )
    elif flow_id:
        # Flow-level root: the whole flow must be terminal first. This is what
        # keeps a multi-step flow alive while an early step is done.
        flow = await _load_flow(str(flow_id), session_id)
        if flow is None:
            # The flow was pruned/removed; there is no running work left to
            # gate. Refusing here would strand the todo forever.
            return
        if str(flow.get("status") or "") not in _TASKFLOW_TERMINAL:
            raise TodoStoreError(
                f"Cannot mark todo completed: TaskFlow flow '{flow_id}' is "
                f"'{flow.get('status')}'. Finish or cancel the flow first."
            )


async def _push_todo_update(session_id: str, todos: list[dict]) -> None:
    """Best-effort ``todo_updated`` WS frame; never raises.

    Mirrors ``server/service/heartbeat.py``: an absent websocket or a failed
    send is logged and swallowed so it cannot break the persistence path.
    """
    try:
        payload = {
            "event": "todo_updated",
            "session_id": session_id,
            "content": {"todos": todos},
        }
        await broadcast_to_session(session_id, payload, ensure_ascii=False)
    except Exception as e:
        logger.warning("Failed to push todo_updated for session {}: {}", session_id, e)


async def _compensate_removed_flow(session_id: str, flow_id: str) -> None:
    """Fail a flow immediately when its explicit root todo was removed.

    The background sweep is the backstop; an explicit ``remove`` should not
    wait for the grace window when no other active todo still references the
    flow. Fail-open: a lookup/write failure leaves the sweep to collect it.
    """
    try:
        from agent.tools.taskflow.registry import store_sqlite as flow_store
        from agent.tools.taskflow.tools._shared import is_terminal

        if flow_id in await store.get_active_flow_ids(session_id):
            return
        flow = await flow_store.get_flow(flow_id, session_id)
        if flow is None or is_terminal(str(flow.get("status") or "")):
            return
        await flow_store.fail_flow_for_gc(flow, reason="todo_removed_explicitly")
    except Exception as exc:  # noqa: BLE001 - the unreferenced-flow sweep is the backstop
        logger.warning("todo remove: immediate flow compensation failed for {}: {}", flow_id, exc)


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

            # SQLite write on an async path: keep the (sub-millisecond but
            # fsync-bound) statement off the event loop.
            await asyncio.to_thread(
                state_register_db.set_state, session_id, _PLAN_REF_STATE_KEY, plan_ref
            )
        latest = await store.get_todos(session_id)
        await _push_todo_update(session_id, latest)
        return latest

    @staticmethod
    async def add_todo(
        session_id: str,
        *,
        content: str,
        flow_id: str | None = None,
        step_id: str | None = None,
        category: str | None = None,
        after_todo_id: int | None = None,
        plan_ref: str | None = None,
    ) -> list[dict]:
        """Insert one pending todo, at the tail or after a stable todo_id.

        Unlike ``update_todos`` this is a single INSERT, so it cannot clobber a
        concurrent full replacement. The category vocabulary is default-
        downgraded like every other write. ``after_todo_id`` is the stable
        anchor for an in-list insertion; None appends at the tail.
        """
        item = _validate_todos(
            [
                {
                    "content": content,
                    "status": "pending",
                    "priority": DEFAULT_PRIORITY,
                    "category": category or DEFAULT_CATEGORY,
                    "flow_id": flow_id,
                    "step_id": step_id,
                    "plan_ref": plan_ref,
                }
            ]
        )[0]
        inserted = await store.add_todo(session_id, item, after_todo_id=after_todo_id)
        if inserted is None:
            raise TodoIdNotFoundError(
                int(after_todo_id or 0),
                await store.get_todos(session_id),
                field="after_todo_id",
            )
        if plan_ref:
            from runtime import state_register_db

            await asyncio.to_thread(
                state_register_db.set_state,
                session_id,
                _PLAN_REF_STATE_KEY,
                plan_ref,
            )
        latest = await store.get_todos(session_id)
        await _push_todo_update(session_id, latest)
        return latest

    @staticmethod
    async def patch_todo(
        session_id: str,
        todo_id: int,
        *,
        content: str | None = None,
        status: str | None = None,
        priority: str | None = None,
        category: str | None = None,
    ) -> dict:
        """Patch one todo by its stable id; reference fields stay untouched."""
        if content is not None and not str(content).strip():
            raise TodoStoreError("Cannot patch todo: content must be a non-empty string")
        normalized_category = category
        if category is not None and category not in VALID_CATEGORIES:
            normalized_category = DEFAULT_CATEGORY
        if status == "completed":
            todo = await store.get_todo_by_id(session_id, todo_id)
            if todo is None:
                raise TodoIdNotFoundError(todo_id, await store.get_todos(session_id))
            await _assert_transition_allowed(todo, session_id)
        updated = await store.patch_todo_by_id(
            session_id,
            todo_id,
            content=content,
            status=status,
            priority=priority,
            category=normalized_category,
        )
        if updated is None:
            raise TodoIdNotFoundError(todo_id, await store.get_todos(session_id))
        await _push_todo_update(session_id, await store.get_todos(session_id))
        return updated

    @staticmethod
    async def remove_todo(session_id: str, todo_id: int) -> dict:
        """Remove one todo by id; a removed flow root is compensated immediately."""
        removed = await store.remove_todo_by_id(session_id, todo_id)
        if removed is None:
            raise TodoIdNotFoundError(todo_id, await store.get_todos(session_id))
        if removed.get("flow_id"):
            await _compensate_removed_flow(session_id, str(removed["flow_id"]))
        await _push_todo_update(session_id, await store.get_todos(session_id))
        return removed

    @staticmethod
    async def reattach_todo(session_id: str, todo_id: int, flow_id: str) -> dict:
        """Bind an existing todo as the GC root of an active flow."""
        flow = await _load_flow(flow_id, session_id)
        if flow is None:
            raise TodoStoreError(f"Cannot reattach: TaskFlow flow '{flow_id}' not found")
        if str(flow.get("status") or "") in _TASKFLOW_TERMINAL:
            raise TodoStoreError(
                f"Cannot reattach: TaskFlow flow '{flow_id}' is terminal "
                f"(status={flow.get('status')})"
            )
        updated = await store.set_todo_flow_id_by_id(session_id, todo_id, flow_id)
        if updated is None:
            raise TodoIdNotFoundError(todo_id, await store.get_todos(session_id))
        await _push_todo_update(session_id, await store.get_todos(session_id))
        return updated

    @staticmethod
    async def list_orphan_flows(session_id: str) -> list[dict]:
        """Active flows in this session that no active todo currently references."""
        from agent.tools.taskflow.registry import store_sqlite as flow_store

        roots = await store.get_active_flow_ids(session_id)
        flows = await flow_store.get_non_terminal_flows(session_id)
        orphans: list[dict] = []
        for flow in flows:
            flow_id = str(flow.get("flow_id") or "")
            if not flow_id or flow_id in roots:
                continue
            state = flow.get("state") or {}
            orphans.append(
                {
                    "flow_id": flow_id,
                    "status": flow.get("status"),
                    "description": str(state.get("description") or ""),
                    "child_session_key": flow.get("child_session_key") or "",
                    "expected_revision": int(flow.get("expected_revision") or 0),
                }
            )
        return orphans

    @staticmethod
    async def get_todos(session_id: str) -> list[dict]:
        """Read the session's todos from the store."""
        return await store.get_todos(session_id)

    @staticmethod
    async def get_active_flow_ids(session_id: str) -> set[str]:
        """The session's GC roots: flow ids referenced by active todos."""
        return await store.get_active_flow_ids(session_id)

    @staticmethod
    async def cancel_todos_for_flows(session_id: str, flow_ids: list[str]) -> int:
        """Cancel active todos whose referenced flows are already terminal-failed."""
        cancelled = await store.cancel_todos_for_flows(session_id, flow_ids)
        if cancelled:
            latest = await store.get_todos(session_id)
            await _push_todo_update(session_id, latest)
        return cancelled


__all__ = [
    "TodoIdNotFoundError",
    "TodoService",
    "TodoStoreError",
    "_TASKFLOW_TERMINAL",
    "_assert_transition_allowed",
    "_compensate_removed_flow",
    "_push_todo_update",
    "_read_taskflow_step_status",
    "_validate_todos",
]
