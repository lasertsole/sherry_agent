"""taskflow_dispatch: batch-dispatch one or more currently-ready steps.

A step is dispatchable when its status is ``ready``, or when it is ``blocked``
but its dependencies are now satisfied (every referenced step is ``done``).
ALL ids are validated before any spawn, so an invalid request is all-or-nothing:
it returns an ``Error:`` and mutates nothing. Spawns happen SEQUENTIALLY through
the shared, monkeypatchable ``_dispatch.dispatch_child`` seam; a partial failure
persists the successful spawns in ONE ``update_flow`` so a spawned child is
never left unrecorded. The flow-level ``child_session_key`` is deliberately NOT
touched by this tool - per-step child keys are authoritative.
"""

import time
from typing import Annotated

from langchain_core.tools import tool
from langgraph.prebuilt.tool_node import InjectedState

from ..config import StepStatus
from ..registry import store_sqlite
from . import _dispatch
from ._retry import is_redispatch, normalize_policy, step_retry_count
from ._shared import (
    apply_dispatched_steps,
    deps_satisfied,
    is_terminal,
    not_found_error,
    requester_session_key,
    step_status,
    terminal_error,
    update_flow_with_conflict_retry,
)

SessionId = Annotated[str, InjectedState("session_id")]


@tool("taskflow_dispatch")
async def taskflow_dispatch(
    flow_id: str,
    step_ids: list[str],
    expected_revision: int | None = None,
    session_id: SessionId = "",
) -> str:
    """Dispatch one or more currently-ready steps of a flow (batch assignment).

    Each step must be ``ready``, or ``blocked`` with its dependencies already
    satisfied. Every id is validated before anything spawns: an unknown id or a
    step that is already dispatched/done rejects the whole call and changes
    nothing. On success all steps become ``dispatched`` with their own child
    session key; a mid-batch spawn failure stops the batch and persists the
    successes so no spawned child is lost. Pass expected_revision to fail fast
    on concurrent writers.

    A step carrying a retry_policy that was already dispatched once consumes
    one retry from its budget (retry_count is incremented); once the budget is
    exhausted the step is rejected before any spawn.
    """
    flow_id = (flow_id or "").strip()
    if not flow_id:
        return "Error: flow_id is required"

    flow = await store_sqlite.get_flow(flow_id)
    if flow is None:
        return not_found_error(flow_id)
    if is_terminal(flow["status"]):
        return terminal_error(flow_id, flow["status"])

    requested = [str(s).strip() for s in (step_ids or [])]
    if not requested:
        return "Error: step_ids is required"

    seen: set[str] = set()
    duplicates: list[str] = []
    for sid in requested:
        if sid in seen:
            duplicates.append(sid)
        else:
            seen.add(sid)
    if duplicates:
        return f"Error: duplicate step_id(s): {', '.join(dict.fromkeys(duplicates))}"

    state = dict(flow["state"])
    steps = list(state.get("steps") or [])
    by_id = {s.get("step_id"): s for s in steps}

    # Validate the WHOLE batch first: any invalid id aborts with no spawn.
    for sid in requested:
        step = by_id.get(sid)
        if step is None:
            return f"Error: unknown step_id '{sid}'"
        status = step_status(step)
        dispatchable = status == StepStatus.READY or (
            status == StepStatus.BLOCKED and deps_satisfied(step, steps)
        )
        if not dispatchable:
            return f"Error: step_id '{sid}' is not dispatchable (status={status})"
        policy = normalize_policy(step)
        if policy is not None and is_redispatch(step):
            count = step_retry_count(step)
            if count >= policy["max_retries"]:
                return (
                    f"Error: step_id '{sid}' retry budget exhausted "
                    f"(retry_count={count}, max_retries={policy['max_retries']})"
                )

    revision = (
        int(expected_revision) if expected_revision is not None else flow["expected_revision"]
    )
    requester_key = requester_session_key(session_id)

    dispatched_ids: list[str] = []
    records: list[dict] = []
    failure: Exception | None = None
    for sid in requested:
        step = by_id[sid]
        try:
            child_key = await _dispatch.dispatch_child(
                task=str(step.get("task") or ""),
                requester_session_key=requester_key,
                label=None,
            )
        except Exception as exc:  # tool boundary: convert to text, persist successes below
            failure = exc
            break
        policy = normalize_policy(step)
        if policy is not None:
            count = step_retry_count(step)
            step["retry_count"] = count + 1 if is_redispatch(step) else count
        step["status"] = str(StepStatus.DISPATCHED)
        step["child_session_key"] = child_key
        step["dispatched_at"] = time.time()
        records.append(step)
        dispatched_ids.append(sid)

    if not records:
        failed_ids = [sid for sid in requested if sid not in dispatched_ids]
        return (
            f"Error: dispatch failed for step_id(s) {failed_ids} "
            f"({type(failure).__name__}: {failure}); dispatched={dispatched_ids}"
        )

    def build_state(fresh_flow: dict, _attempt: int) -> dict:
        fresh_state = dict(fresh_flow["state"])
        fresh_steps = list(fresh_state.get("steps") or [])
        fresh_state["steps"] = apply_dispatched_steps(fresh_steps, records)
        return fresh_state

    updated, error = await update_flow_with_conflict_retry(
        flow_id,
        revision,
        flow,
        build_state,
        child_keys=[str(record.get("child_session_key") or "") for record in records],
    )
    if updated is None:
        return error

    failed_ids = [sid for sid in requested if sid not in dispatched_ids]
    if failure is not None:
        return (
            f"Error: dispatch failed for step_id(s) {failed_ids} "
            f"({type(failure).__name__}: {failure}); dispatched={dispatched_ids}"
        )

    return (
        f"TaskFlow steps dispatched: flow_id={flow_id}, step_ids={dispatched_ids}, "
        f"revision={updated['expected_revision']}"
    )
