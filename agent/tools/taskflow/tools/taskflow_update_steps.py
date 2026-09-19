"""taskflow_update_steps: full replacement of a flow's steps list.

Like ``todowrite`` for TaskFlow: the caller passes the COMPLETE step list and
the stored DAG becomes exactly that list - steps can be added, removed,
reordered, or have their task/depends_on rewritten. Already-running work is
protected by six safety rules:

1. ``step_id`` is unique within the list; every ``depends_on`` entry must
   exist in the NEW list (self-consistency) and no step may depend on itself.
2. A step that is ``dispatched`` cannot be downgraded to ``ready``/``blocked``;
   its ``child_session_key`` is retained. Its task text may change, but the
   running child keeps the task it was spawned with.
3. A step that is ``done`` cannot change its task, depends_on, or status (its
   result was already injected); its ``child_session_key`` is retained.
4. Fabricated dispatch/execution states are rejected: a brand-new step must be
   ``ready`` or ``blocked``, and a ready/blocked step stays in that plane.
   Dispatch goes through ``taskflow_dispatch``, completion through
   ``taskflow_resume``.
5. Terminal flows (done/failed/cancelled) reject the call.
6. Removing a ``dispatched`` step whose child is still running is allowed but
   reported as a non-blocking ``Warning:`` - the child keeps running, so the
   caller should kill it or settle it via taskflow_wait_all/taskflow_resume.

Concurrency uses the store's optimistic lock: pass ``expected_revision`` to
fail fast, and a mismatch returns the latest revision for re-read + retry.
"""

from collections.abc import Callable
from typing import Annotated, Any

from langchain_core.tools import tool
from langgraph.prebuilt.tool_node import InjectedState

from ..config import StepStatus
from ..registry import store_sqlite
from ..registry.store_sqlite import FlowConflictError, FlowNotFoundError
from ._shared import (
    conflict_error,
    is_terminal,
    not_found_error,
    step_status,
    terminal_error,
    validate_steps_list,
)

SessionId = Annotated[str, InjectedState("session_id")]

# The only statuses a caller may assign to a step in the structural plane.
_STRUCTURAL_STATUSES = frozenset({StepStatus.READY.value, StepStatus.BLOCKED.value})

# Injectable subagent-registry seam (mirrors taskflow_wait_all): bound lazily on
# the first orphan check and replaceable in tests, so the family stays
# importable without the spawn pipeline.
get_run_by_child_session_key: Callable[[str], Any] | None = None
is_live_unended_run: Callable[[Any], bool] | None = None


def _ensure_registry() -> str | None:
    """Bind the child-liveness seam on first use; return error text when absent."""
    global get_run_by_child_session_key, is_live_unended_run
    if get_run_by_child_session_key is not None and is_live_unended_run is not None:
        return None
    try:
        from agent.tools.subagent.registry.helpers import (
            is_live_unended_run as _real_is_live,
        )
        from agent.tools.subagent.registry.queries import (
            get_run_by_child_session_key as _real_get_run,
        )
    except Exception as exc:  # a missing registry degrades to a warning, never a raise
        return f"{type(exc).__name__}: {exc}"
    get_run_by_child_session_key = _real_get_run
    is_live_unended_run = _real_is_live
    return None


def _normalize_steps(steps: list[dict]) -> list[dict]:
    """Normalize the contract fields (strip whitespace), keeping caller extras."""
    return [
        {
            **step,
            "step_id": str(step["step_id"]).strip(),
            "task": str(step["task"]).strip(),
            "depends_on": [str(dep).strip() for dep in step.get("depends_on") or []],
            "status": str(step["status"]),
        }
        for step in steps
    ]


def _merge_steps(
    existing: list[dict], incoming: list[dict]
) -> tuple[list[dict] | None, str | None]:
    """Apply the cross-step safety rules to a full replacement.

    Steps are matched by ``step_id``. Unmatched incoming entries are new steps
    (only ``ready``/``blocked`` are legal). For matched steps the previous
    status decides which immutability rules run. Returns ``(merged, None)`` on
    success or ``(None, error_text)`` when a safety rule rejects the call.
    """
    old_by_id = {s.get("step_id"): s for s in existing if s.get("step_id") is not None}
    merged: list[dict] = []
    for step in incoming:
        sid = step["step_id"]
        old = old_by_id.get(sid)
        new = dict(step)
        if old is None:
            if new["status"] not in _STRUCTURAL_STATUSES:
                return None, (
                    f"Error: new step '{sid}' must be ready or blocked "
                    f"(got status={new['status']}); dispatch through taskflow_dispatch"
                )
            merged.append(new)
            continue
        old_status = step_status(old)
        new_status = new["status"]
        if old_status == StepStatus.DISPATCHED.value:
            if new_status in _STRUCTURAL_STATUSES:
                return None, (
                    f"Error: step '{sid}' is dispatched and cannot be downgraded "
                    f"to status={new_status}"
                )
        elif old_status == StepStatus.DONE.value:
            if new_status != StepStatus.DONE.value:
                return None, (
                    f"Error: step '{sid}' is done; its status cannot be changed "
                    f"(got status={new_status})"
                )
            if new["task"] != str(old.get("task") or "").strip():
                return None, f"Error: step '{sid}' is done; its task cannot be changed"
            old_deps = sorted(str(dep).strip() for dep in old.get("depends_on") or [])
            if sorted(new["depends_on"]) != old_deps:
                return None, f"Error: step '{sid}' is done; its depends_on cannot be changed"
        elif new_status not in _STRUCTURAL_STATUSES:
            return None, (
                f"Error: step '{sid}' (status={old_status}) cannot become status={new_status}; "
                "use taskflow_dispatch or taskflow_resume"
            )
        if old_status in (StepStatus.DISPATCHED.value, StepStatus.DONE.value):
            old_key = str(old.get("child_session_key") or "").strip()
            provided_key = str(new.get("child_session_key") or "").strip()
            if provided_key and provided_key != old_key:
                return None, (
                    f"Error: step '{sid}' is {old_status}; its child_session_key is immutable "
                    f"(stored={old_key or '(none)'}, provided={provided_key})"
                )
            if not old_key:
                return None, (
                    f"Error: step '{sid}' is {old_status} but has no recorded child_session_key"
                )
            new["child_session_key"] = old_key
            if new.get("dispatched_at") is None and old.get("dispatched_at") is not None:
                new["dispatched_at"] = old["dispatched_at"]
        merged.append(new)
    return merged, None


def _orphan_warnings(removed_dispatched: list[dict]) -> list[str]:
    """Warning lines for removed dispatched steps whose children may still run."""
    if not removed_dispatched:
        return []
    registry_error = _ensure_registry()
    if get_run_by_child_session_key is None or is_live_unended_run is None:
        return [
            "Warning: removed dispatched step(s) but the subagent registry is unavailable "
            f"({registry_error}); verify the removed children manually"
        ]
    warnings: list[str] = []
    for step in removed_dispatched:
        step_id = step.get("step_id")
        child_key = str(step.get("child_session_key") or "")
        try:
            run = get_run_by_child_session_key(child_key)
            live = run is not None and bool(is_live_unended_run(run))
        except Exception as exc:  # registry lookup is an external seam
            warnings.append(
                f"Warning: orphan check failed for removed step_id={step_id} "
                f"({type(exc).__name__}: {exc})"
            )
            continue
        if live:
            warnings.append(
                f"Warning: removed step_id={step_id} still has a running child "
                f"(child_session_key={child_key}); the child was NOT stopped - call "
                "sessions_kill, or settle it with taskflow_wait_all/taskflow_resume"
            )
    return warnings


@tool("taskflow_update_steps")
async def taskflow_update_steps(
    flow_id: str,
    steps: list[dict],
    expected_revision: int | None = None,
    session_id: SessionId = "",
) -> str:
    """Full-replace the steps list of a flow (like todowrite for TaskFlow).

    Pass the COMPLETE steps list; the stored list becomes exactly what you pass
    (plus retained child keys). Each step must be an object with: ``step_id``
    (unique in the list), ``task`` (non-empty), ``depends_on`` (list of step_ids
    that must exist in this same list), and ``status`` (``ready`` / ``blocked``
    / ``dispatched`` / ``done``). Use it to add, remove, reorder, or rewrite
    steps - new steps must be ``ready`` or ``blocked``; dispatch goes through
    taskflow_dispatch.

    Safety rules: a step that was ``dispatched`` keeps its child_session_key and
    cannot be downgraded to ready/blocked (its task may change, but the running
    child keeps the old task). A step that was ``done`` cannot change its task,
    depends_on, or status. Removing a dispatched step whose child is still
    running returns a non-blocking warning (kill it or settle it). Terminal
    flows (done/failed/cancelled) reject the call. Pass expected_revision to
    fail fast on concurrent writers; a mismatch returns the latest revision.
    """
    flow_id = (flow_id or "").strip()
    if not flow_id:
        return "Error: flow_id is required"
    validation_error = validate_steps_list(steps)
    if validation_error is not None:
        return validation_error

    flow = await store_sqlite.get_flow(flow_id, session_id)
    if flow is None:
        return not_found_error(flow_id)
    if is_terminal(flow["status"]):
        return terminal_error(flow_id, flow["status"])

    state = dict(flow["state"])
    existing = list(state.get("steps") or [])
    incoming = _normalize_steps(list(steps))

    merged, merge_error = _merge_steps(existing, incoming)
    if merged is None:
        return merge_error or "Error: steps update rejected"

    merged_ids = {step["step_id"] for step in merged}
    old_by_id = {s.get("step_id"): s for s in existing if s.get("step_id") is not None}
    added_ids = [step["step_id"] for step in merged if step["step_id"] not in old_by_id]
    removed = [
        s for s in existing if s.get("step_id") is not None and s["step_id"] not in merged_ids
    ]
    removed_ids = [str(step.get("step_id")) for step in removed]
    removed_dispatched = [
        step for step in removed if step_status(step) == StepStatus.DISPATCHED.value
    ]

    revision = (
        int(expected_revision) if expected_revision is not None else flow["expected_revision"]
    )
    state["steps"] = merged
    try:
        updated = await store_sqlite.update_flow(
            flow_id, revision, session_id=session_id, state=state
        )
    except FlowConflictError as exc:
        return conflict_error(exc)
    except FlowNotFoundError:
        return not_found_error(flow_id)

    lines = [
        f"TaskFlow steps updated: flow_id={flow_id}, steps={len(merged)}, "
        f"added=[{','.join(added_ids)}], removed=[{','.join(removed_ids)}], "
        f"revision={updated['expected_revision']}"
    ]
    lines.extend(_orphan_warnings(removed_dispatched))
    return "\n".join(lines)
