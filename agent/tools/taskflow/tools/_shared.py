"""Shared plumbing for the taskflow tool family (internal module).

Error-text contract: tools NEVER raise business errors to the LLM; they
return human-readable strings prefixed with ``Error:``. Conflict texts embed
the latest revision so the caller can re-read (taskflow_summary) and retry
with the freshest expected_revision, per skills/builtin/core/taskflow/SKILL.md.
"""

from collections.abc import Callable

from ..config import TERMINAL_STATUSES, StepStatus
from ..registry import store_sqlite
from ..registry.store_sqlite import FlowConflictError, FlowNotFoundError, UNSET

# Bounded optimistic-lock retries for a mutation whose side effect (a spawned
# child session) already happened: losing the write must never drop the child.
PERSIST_MAX_ATTEMPTS = 3


def requester_session_key(session_id: str) -> str:
    """Build the canonical requester session key from a raw LangGraph session id."""
    return f"agent:main:session:{session_id}"


def default_state(description: str, initial_state: dict | None) -> dict:
    """Build the initial state_json payload with guaranteed invariants.

    ``steps`` and ``results`` are always lists (run_task / resume append to
    them); ``description`` and any caller keys are preserved.
    """
    state = dict(initial_state or {})
    state["description"] = description or str(state.get("description") or "")
    state["steps"] = list(state.get("steps") or [])
    state["results"] = list(state.get("results") or [])
    return state


def not_found_error(flow_id: str) -> str:
    return f"Error: TaskFlow '{flow_id}' not found"


def terminal_error(flow_id: str, status: str) -> str:
    return (
        f"Error: TaskFlow '{flow_id}' is terminal (status={status}); no further mutations allowed"
    )


def conflict_error(exc: FlowConflictError) -> str:
    return f"Error: {exc}"


def is_terminal(status: str) -> bool:
    return status in TERMINAL_STATUSES


def new_step(
    step_id: str,
    task: str,
    depends_on: list[str] | None = None,
    status: StepStatus | str = StepStatus.READY,
) -> dict:
    """Build a step dict for the flow's ``steps`` list.

    All DAG fields live inside ``state_json`` (no DB migration): a stable
    ``step_id``, the task text, the dependency ids, and the current status.
    """
    return {
        "step_id": step_id,
        "task": task,
        "depends_on": list(depends_on or []),
        "status": str(status),
    }


def step_status(step: dict) -> str:
    """Return a step's status, deriving it for pre-DAG legacy steps.

    A legacy step has no ``status`` field; it was dispatched iff it already
    carries a ``child_session_key``, otherwise it is still ready.
    """
    status = step.get("status")
    if status:
        return str(status)
    if step.get("child_session_key"):
        return str(StepStatus.DISPATCHED)
    return str(StepStatus.READY)


def deps_satisfied(step: dict, steps: list[dict]) -> bool:
    """True iff every ``depends_on`` id exists in ``steps`` and is ``done``.

    Missing/empty ``depends_on`` is trivially satisfied. An unknown dep id is
    never satisfied. A self-dependency is never satisfied, which also makes a
    self-referential step safe to leave blocked without an unlock loop.
    """
    depends_on = step.get("depends_on")
    if not depends_on:
        return True
    step_id = step.get("step_id")
    by_id = {s.get("step_id"): s for s in steps}
    for dep_id in depends_on:
        if dep_id == step_id:
            return False
        dep = by_id.get(dep_id)
        if dep is None:
            return False
        if step_status(dep) != StepStatus.DONE:
            return False
    return True


def mark_step_done(steps: list[dict], child_session_key: str) -> str | None:
    """Mark the step matching ``child_session_key`` done; return its id.

    Returns ``None`` when no step carries that child key. Idempotent: a
    repeated call for the same key returns the same id and leaves the step done.
    """
    if not child_session_key:
        return None
    for step in steps:
        if step.get("child_session_key") == child_session_key:
            step["status"] = str(StepStatus.DONE)
            return step.get("step_id")
    return None


def unlock_dependents(steps: list[dict]) -> list[str]:
    """Move blocked steps with satisfied deps to ready; return their ids.

    A single pass, so a self-dependency or a dependency cycle can never loop.
    """
    newly_ready: list[str] = []
    for step in steps:
        if step_status(step) != StepStatus.BLOCKED:
            continue
        if deps_satisfied(step, steps):
            step["status"] = str(StepStatus.READY)
            step_id = step.get("step_id")
            if step_id is not None:
                newly_ready.append(step_id)
    return newly_ready


def record_unpersisted_children_error(flow_id: str, child_keys: list[str], attempts: int) -> str:
    """Error text naming spawned children that could not be persisted.

    A spawned child is already running: the caller must recover it by key and
    must NOT re-dispatch it. This text is the only trace of an unrecorded child,
    so it always names every key.
    """
    keys = ", ".join(key for key in child_keys if key)
    return (
        f"Error: TaskFlow '{flow_id}': spawned child session(s) could not be recorded "
        f"after {attempts} optimistic-lock attempt(s): child_session_key(s)=[{keys}]. "
        "The child session(s) are already running - do NOT dispatch them again; "
        "recover using the key(s) above."
    )


def apply_dispatched_steps(fresh_steps: list[dict], records: list[dict]) -> list[dict]:
    """Re-apply recorded dispatched-step payloads onto a freshly-read step list.

    Matching is by ``step_id``. A record whose step is absent from the fresh
    state is appended, so a spawned child is never lost to a concurrent writer
    that rewrote the step list.
    """
    merged = list(fresh_steps)
    by_id = {s.get("step_id"): s for s in merged if s.get("step_id") is not None}
    for record in records:
        step_id = record.get("step_id")
        target = by_id.get(step_id) if step_id is not None else None
        if target is None:
            merged.append(dict(record))
            continue
        target["status"] = record["status"]
        target["child_session_key"] = record["child_session_key"]
        target["dispatched_at"] = record["dispatched_at"]
    return merged


async def update_flow_with_conflict_retry(
    flow_id: str,
    expected_revision: int,
    origin_flow: dict,
    build_state: Callable[[dict, int], dict],
    *,
    child_keys: list[str],
    flow_child_session_key: object = UNSET,
    max_attempts: int = PERSIST_MAX_ATTEMPTS,
) -> tuple[dict | None, str | None]:
    """Persist a mutation, re-reading and rebuilding state on optimistic-lock conflict.

    A child session that has ALREADY spawned must never be silently dropped: the
    caller invokes this only after a successful spawn, and when the write loses
    the race ``build_state(fresh_flow, attempt)`` is invoked again against the
    freshly-read flow and retried up to ``max_attempts``. A vanished or terminal
    flow is terminal for the retry (the child keys are still named).

    Returns ``(updated_flow, error_text)`` with exactly one non-None: on success
    the updated flow; on exhaustion an error naming every ``child_keys`` entry.
    """
    revision = int(expected_revision)
    current_flow = origin_flow
    for attempt in range(1, max_attempts + 1):
        state = build_state(current_flow, attempt)
        try:
            updated = await store_sqlite.update_flow(
                flow_id,
                revision,
                state=state,
                child_session_key=flow_child_session_key,
            )
            return updated, None
        except FlowConflictError:
            if attempt == max_attempts:
                break
            fresh = await store_sqlite.get_flow(flow_id)
            if fresh is None or is_terminal(fresh["status"]):
                return None, record_unpersisted_children_error(flow_id, child_keys, attempt)
            current_flow = fresh
            revision = int(fresh["expected_revision"])
        except FlowNotFoundError:
            return None, record_unpersisted_children_error(flow_id, child_keys, attempt)
    return None, record_unpersisted_children_error(flow_id, child_keys, max_attempts)


def steps_summary(steps: list[dict]) -> dict[str, int]:
    """Count steps per status, always including all four statuses (zero-filled)."""
    counts = {status.value: 0 for status in StepStatus}
    for step in steps:
        status = step_status(step)
        counts[status] = counts.get(status, 0) + 1
    return counts
