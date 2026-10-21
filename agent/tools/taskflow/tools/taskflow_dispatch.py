"""taskflow_dispatch: batch-dispatch one or more currently-ready steps.

A step is dispatchable when its status is ``ready``, or when it is ``blocked``
but its dependencies are now satisfied (every referenced step is ``done``).
ALL ids are validated before any spawn, so an invalid request is all-or-nothing:
it returns an ``Error:`` and mutates nothing. Spawns happen CONCURRENTLY through
the shared, monkeypatchable ``_dispatch.dispatch_child`` seam, bounded by
``TASKFLOW_INFRA["max_parallel_dispatch"]``; a partial failure persists every
successful spawn in ONE ``update_flow`` (so a spawned child is never left
unrecorded) and names every failed step id. The flow-level
``child_session_key`` is deliberately NOT touched by this tool - per-step child
keys are authoritative.
"""

import asyncio
import time

from langchain_core.tools import tool

from config.features import TASKFLOW_INFRA
from ..config import StepStatus
from ..registry import store_sqlite
from . import _dispatch
from ._retry import (
    is_redispatch,
    normalize_policy,
    step_retry_count,
    step_spawn_kwargs,
    with_judge_feedback,
)
from agent.tools.pub_base import SessionId
from ._shared import (
    build_task_with_bindings,
    apply_dispatched_steps,
    build_task_with_dep_results,
    deps_satisfied,
    is_terminal,
    not_found_error,
    requester_session_key,
    step_status,
    terminal_error,
    update_flow_with_conflict_retry,
)


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
    session key; the batch spawns concurrently and when one spawn fails the
    others still complete — the successes are persisted and the failures are
    reported by step id. Pass expected_revision to fail fast on concurrent
    writers.

    A step carrying a retry_policy that was already dispatched once consumes
    one retry from its budget (retry_count is incremented); once the budget is
    exhausted the step is rejected before any spawn.

    The whole read → validate → spawn → persist sequence runs under the
    flow-local dispatch lock, so a concurrent manual dispatch (or a resume's
    auto-dispatch wave) sees the winner's ``dispatched`` status and refuses to
    spawn the same step twice.
    """
    flow_id = (flow_id or "").strip()
    if not flow_id:
        return "Error: flow_id is required"
    async with _dispatch.flow_dispatch_lock(flow_id):
        return await _taskflow_dispatch_locked(
            flow_id=flow_id,
            step_ids=step_ids,
            expected_revision=expected_revision,
            session_id=session_id,
        )


async def _taskflow_dispatch_locked(
    flow_id: str,
    step_ids: list[str],
    expected_revision: int | None = None,
    session_id: SessionId = "",
) -> str:
    """Body of the dispatch tool; the caller already holds the flow lock."""
    flow_id = (flow_id or "").strip()
    if not flow_id:
        return "Error: flow_id is required"

    flow = await store_sqlite.get_flow(flow_id, session_id)
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
    results = list(state.get("results") or [])
    by_id = {s.get("step_id"): s for s in steps}

    # Validate the WHOLE batch first: any invalid id aborts with no spawn.
    for sid in requested:
        step = by_id.get(sid)
        if step is None:
            return f"Error: unknown step_id '{sid}'"
        status = step_status(step)
        dispatchable = status == StepStatus.READY or (
            status == StepStatus.BLOCKED
            and bool(step.get("depends_on"))
            and deps_satisfied(step, steps)
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
    failures: list[tuple[str, Exception]] = []

    async def _spawn_one(sid: str) -> tuple[str, str | None, Exception | None]:
        """Spawn one step's child; never raises — a failure is returned."""
        step = by_id[sid]
        feedback = str(step.get("judge_feedback") or "").strip()
        task_text = with_judge_feedback(
            build_task_with_bindings(
                build_task_with_dep_results(step, steps, results), step, steps, results
            ),
            feedback,
        )
        try:
            child_key = await _dispatch.dispatch_child(
                task=task_text,
                requester_session_key=requester_key,
                label=None,
                # The step's own spawn contract rides along (schema, role, model,
                # timeout): a step registered by taskflow_plan / update_steps must
                # reach its child with the same configuration run_task would give.
                **step_spawn_kwargs(step),
            )
        except Exception as exc:  # tool boundary: report; the other spawns stand
            return sid, None, exc
        return sid, child_key, None

    # The wave spawns CONCURRENTLY, bounded by ``max_parallel_dispatch`` (the
    # SUBAGENT lane queues anything over its own cap as PENDING, so this only
    # decides how many spawn calls are in flight at once). One step failing no
    # longer aborts the rest — every successful child is persisted below and
    # every failure is reported by step id.
    semaphore = asyncio.Semaphore(max(1, int(TASKFLOW_INFRA["max_parallel_dispatch"])))

    async def _bounded(sid: str) -> tuple[str, str | None, Exception | None]:
        async with semaphore:
            return await _spawn_one(sid)

    spawned = await asyncio.gather(*(_bounded(sid) for sid in requested))

    for sid, child_key, exc in spawned:
        if exc is not None or child_key is None:
            failures.append((sid, exc or RuntimeError("dispatch returned no child key")))
            continue
        step = by_id[sid]
        feedback = str(step.get("judge_feedback") or "").strip()
        if feedback:
            step.pop("judge_feedback", None)
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
        failed_ids = [sid for sid, _ in failures]
        first = failures[0][1]
        return (
            f"Error: dispatch failed for step_id(s) {failed_ids} "
            f"({type(first).__name__}: {first}); dispatched={dispatched_ids}"
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
        session_id=session_id,
    )
    if updated is None:
        return error

    if failures:
        failed_text = "; ".join(f"{sid} ({type(exc).__name__}: {exc})" for sid, exc in failures)
        return f"Error: dispatch failed for step_id(s) {failed_text}; dispatched={dispatched_ids}"

    return (
        f"TaskFlow steps dispatched: flow_id={flow_id}, step_ids={dispatched_ids}, "
        f"revision={updated['expected_revision']}"
    )
