"""taskflow_resume: inject a child session result into the flow state (openclaw resume).

Idempotency: the (child_session_key, result) pair is fingerprinted; resuming
with the SAME pair again is a no-op that neither re-injects nor bumps the
revision, so announce-pipeline redeliveries cannot corrupt the state.

Failure-aware retry (GAP-8): when the step carries a retry_policy and the
result text classifies as a failure allowed by ``retry_on``, the step is
re-dispatched instead of marked done - the failure result is still recorded.
"""

import time
from typing import Annotated

from langchain_core.tools import tool
from langgraph.prebuilt.tool_node import InjectedState

from ..config import StepStatus, TaskFlowStatus
from ..registry import store_sqlite
from ..registry.store_sqlite import FlowConflictError, FlowNotFoundError
from ._retry import (
    apply_redispatch,
    normalize_policy,
    requester_key_for_retry,
    should_retry_failure,
    spawn_replacement,
    step_retry_count,
    wait_before_retry,
)
from ._shared import (
    conflict_error,
    is_terminal,
    not_found_error,
    result_hash,
    steps_summary,
    terminal_error,
    unlock_dependents,
    update_flow_with_conflict_retry,
)

SessionId = Annotated[str, InjectedState("session_id")]

_STATUS_ORDER = tuple(status.value for status in StepStatus)


@tool("taskflow_resume")
async def taskflow_resume(
    flow_id: str,
    child_session_key: str = "",
    result: str = "",
    expected_revision: int | None = None,
    token_usage: dict | None = None,
    validation_criteria: str | None = None,
    session_id: SessionId = "",
) -> str:
    """Inject a completed child session result into the flow state (idempotent).

    Appends {child_session_key, result} to the flow's results and returns the
    flow to running when it was waiting. Resuming with the SAME
    child_session_key and result again is a no-op (already resumed), so
    duplicate deliveries never inject twice. Pass expected_revision to fail
    fast on concurrent writers. Pass
    token_usage={"input_tokens": N, "output_tokens": M, "model_name": "..."} to
    accumulate the child's token spend and estimated cost onto the flow.

    When the step carries validation_criteria (set by taskflow_run_task or
    passed here), the response echoes them with a warning that the result
    still needs validation: the orchestrator judges the result against the
    criteria. The criteria are not enforced by the tool.

    When the result text classifies as a failure and the step's retry_policy
    allows it (``retry_on`` filter, budget left), a replacement child is
    spawned and the step stays dispatched on the new child; otherwise the step
    is marked done. ``session_id`` is injected by the runtime and used as the
    fallback requester for a replacement child.
    """
    flow_id = (flow_id or "").strip()
    child_session_key = (child_session_key or "").strip()
    result = result or ""
    if not flow_id:
        return "Error: flow_id is required"
    if not child_session_key and not result:
        return "Error: taskflow_resume requires child_session_key or result"

    flow = await store_sqlite.get_flow(flow_id)
    if flow is None:
        return not_found_error(flow_id)
    if is_terminal(flow["status"]):
        return terminal_error(flow_id, flow["status"])

    revision = (
        int(expected_revision) if expected_revision is not None else flow["expected_revision"]
    )

    result_hash_value = result_hash(child_session_key, result)
    state = dict(flow["state"])
    results = list(state.get("results") or [])
    if any(isinstance(r, dict) and r.get("result_hash") == result_hash_value for r in results):
        return (
            f"TaskFlow resume skipped (already resumed): flow_id={flow_id}, "
            f"result_hash={result_hash_value}, results={len(results)}"
        )

    result_record = {
        "child_session_key": child_session_key,
        "result": result,
        "result_hash": result_hash_value,
        "injected_at": time.time(),
    }
    results.append(result_record)
    state["results"] = results

    # DAG bookkeeping: a retried failure keeps the step dispatched on its
    # replacement child; any other result marks the step done and may unlock
    # blocked dependents. Resume never spawns the newly-ready steps.
    steps = list(state.get("steps") or [])
    step = next((s for s in steps if s.get("child_session_key") == child_session_key), None)
    step_id = step.get("step_id") if step is not None else None
    retry_text = ""
    redispatched_key: str | None = None
    retry_count_after: int | None = None
    if step is not None:
        policy = normalize_policy(step)
        if policy is not None and should_retry_failure(step, result):
            requester_key = requester_key_for_retry(state, session_id)
            await wait_before_retry(policy)
            try:
                redispatched_key = await spawn_replacement(
                    str(step.get("task") or ""), requester_key
                )
            except Exception as exc:
                retry_text = (
                    f"\n  retry: re-dispatch failed for step_id={step_id} "
                    f"({type(exc).__name__}: {exc}); step left done with the failure result"
                )
            else:
                retry_count_after = step_retry_count(step) + 1
                apply_redispatch(step, redispatched_key, retry_count_after)
                retry_text = (
                    f"\n  retry: re-dispatched step_id={step_id}, "
                    f"child_session_key={redispatched_key}, "
                    f"retry_count={retry_count_after}/{policy['max_retries']}, "
                    f"retry_delay_seconds={policy['retry_delay_seconds']}"
                )

    validation_text = ""
    if step is not None and redispatched_key is None:
        step["status"] = str(StepStatus.DONE)
        criteria = (validation_criteria or "").strip()
        if criteria:
            step["validation_criteria"] = criteria
        stored_criteria = str(step.get("validation_criteria") or "").strip()
        if stored_criteria:
            validation_text = (
                f"\n  validation_criteria: {stored_criteria}"
                f"\n  ⚠ Result needs validation against criteria"
            )
    newly_ready = unlock_dependents(steps)
    state["steps"] = steps
    counts = steps_summary(steps)

    # Resuming a waiting flow returns it to running; a running flow stays running.
    new_status = (
        TaskFlowStatus.RUNNING.value if flow["status"] == TaskFlowStatus.WAITING.value else None
    )

    # Aggregate the child's token spend when the caller reports it. The
    # idempotency guard above already returned for a duplicate delivery, so a
    # redelivered result never double-counts.
    total_tokens: int | None = None
    total_cost: float | None = None
    if token_usage:
        from config.features import MODEL_PRICING

        input_tokens = int(token_usage.get("input_tokens") or 0)
        output_tokens = int(token_usage.get("output_tokens") or 0)
        pricing_table = MODEL_PRICING["model_pricing_per_m_tokens"]
        model_name = str(token_usage.get("model_name") or "")
        pricing = pricing_table.get(model_name, pricing_table["_default"])

        total_tokens = int(flow.get("total_tokens") or 0) + input_tokens + output_tokens
        cost_delta = (
            input_tokens * pricing["input"] / 1_000_000
            + output_tokens * pricing["output"] / 1_000_000
        )
        total_cost = round(float(flow.get("total_cost") or 0.0) + cost_delta, 6)

    if redispatched_key is not None:
        replacement_key = redispatched_key
        replacement_count = retry_count_after or 0

        def build_state(fresh_flow: dict, _attempt: int) -> dict:
            fresh_state = dict(fresh_flow["state"])
            fresh_results = list(fresh_state.get("results") or [])
            if not any(
                isinstance(r, dict) and r.get("result_hash") == result_hash_value
                for r in fresh_results
            ):
                fresh_results.append(result_record)
            fresh_steps = list(fresh_state.get("steps") or [])
            target = next((s for s in fresh_steps if s.get("step_id") == step_id), None)
            if target is not None:
                apply_redispatch(target, replacement_key, replacement_count)
            fresh_state["results"] = fresh_results
            fresh_state["steps"] = fresh_steps
            return fresh_state

        updated, error = await update_flow_with_conflict_retry(
            flow_id,
            revision,
            flow,
            build_state,
            child_keys=[replacement_key],
            update_kwargs={
                "wait": None,
                "status": new_status,
                "total_tokens": total_tokens,
                "total_cost": total_cost,
            },
        )
        if updated is None:
            return error
    else:
        try:
            updated = await store_sqlite.update_flow(
                flow_id,
                revision,
                state=state,
                wait=None,
                status=new_status,
                total_tokens=total_tokens,
                total_cost=total_cost,
            )
        except FlowConflictError as exc:
            return conflict_error(exc)
        except FlowNotFoundError:
            return not_found_error(flow_id)

    counts_text = ",".join(f"{status}={counts[status]}" for status in _STATUS_ORDER)
    unlocked_text = ",".join(newly_ready)
    return (
        f"TaskFlow resumed: flow_id={flow_id}, revision={updated['expected_revision']}, "
        f"results={len(results)}, status={updated['status']}, step_id={step_id}, "
        f"unlocked=[{unlocked_text}], step_statuses={counts_text}"
        f"{validation_text}"
        f"{retry_text}"
    )
