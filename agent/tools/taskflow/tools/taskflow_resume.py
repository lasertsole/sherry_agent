"""taskflow_resume: inject a child session result into the flow state (openclaw resume).

Idempotency: the (child_session_key, result) pair is fingerprinted; resuming
with the SAME pair again is a no-op that neither re-injects nor bumps the
revision, so announce-pipeline redeliveries cannot corrupt the state.
"""

import hashlib
import time

from langchain_core.tools import tool

from ..config import StepStatus, TaskFlowStatus
from ..registry import store_sqlite
from ..registry.store_sqlite import FlowConflictError, FlowNotFoundError
from ._shared import (
    conflict_error,
    is_terminal,
    mark_step_done,
    not_found_error,
    steps_summary,
    terminal_error,
    unlock_dependents,
)

_STATUS_ORDER = tuple(status.value for status in StepStatus)


def _result_hash(child_session_key: str, result: str) -> str:
    payload = f"{child_session_key}\x1f{result}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


@tool("taskflow_resume")
async def taskflow_resume(
    flow_id: str,
    child_session_key: str = "",
    result: str = "",
    expected_revision: int | None = None,
    token_usage: dict | None = None,
) -> str:
    """Inject a completed child session result into the flow state (idempotent).

    Appends {child_session_key, result} to the flow's results and returns the
    flow to running when it was waiting. Resuming with the SAME
    child_session_key and result again is a no-op (already resumed), so
    duplicate deliveries never inject twice. Pass expected_revision to fail
    fast on concurrent writers. Pass
    token_usage={"input_tokens": N, "output_tokens": M, "model_name": "..."} to
    accumulate the child's token spend and estimated cost onto the flow.
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

    result_hash = _result_hash(child_session_key, result)
    state = dict(flow["state"])
    results = list(state.get("results") or [])
    if any(isinstance(r, dict) and r.get("result_hash") == result_hash for r in results):
        return (
            f"TaskFlow resume skipped (already resumed): flow_id={flow_id}, "
            f"result_hash={result_hash}, results={len(results)}"
        )

    results.append(
        {
            "child_session_key": child_session_key,
            "result": result,
            "result_hash": result_hash,
            "injected_at": time.time(),
        }
    )
    state["results"] = results

    # DAG bookkeeping: this child finishing marks its step done and may unlock
    # blocked dependents. Resume NEVER spawns the newly-ready steps - the
    # caller dispatches them explicitly (taskflow_dispatch).
    steps = list(state.get("steps") or [])
    step_id = mark_step_done(steps, child_session_key)
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
    )
