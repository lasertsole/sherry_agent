"""taskflow_cancel: cancel the flow (openclaw requestCancel / cancel)."""

from typing import Annotated

from langchain_core.tools import tool
from langgraph.prebuilt.tool_node import InjectedState

from ..config import StepStatus, TaskFlowStatus
from ..registry import store_sqlite
from ..progress_push import push_taskflow_progress
from ..registry.store_sqlite import FlowConflictError, FlowNotFoundError
from ._shared import conflict_error, is_terminal, not_found_error, step_status, terminal_error

SessionId = Annotated[str, InjectedState("session_id")]


@tool("taskflow_cancel")
async def taskflow_cancel(
    flow_id: str,
    reason: str = "",
    expected_revision: int | None = None,
    session_id: SessionId = "",
) -> str:
    """Cancel the flow; terminal, no further mutations are accepted.

    Records the cancellation reason in the flow state. Already-dispatched
    child sessions keep running; their results are still deliverable via
    taskflow_resume until the flow was cancelled. Pass expected_revision to
    fail fast on concurrent writers.
    """
    flow_id = (flow_id or "").strip()
    if not flow_id:
        return "Error: flow_id is required"

    flow = await store_sqlite.get_flow(flow_id, session_id)
    if flow is None:
        return not_found_error(flow_id)
    if is_terminal(flow["status"]):
        return terminal_error(flow_id, flow["status"])

    revision = (
        int(expected_revision) if expected_revision is not None else flow["expected_revision"]
    )

    state = dict(flow["state"])
    if reason:
        state["cancel_reason"] = reason

    # Steps that never produced a result are cancelled with the flow: their DAG
    # status must not keep saying ready/dispatched/blocked after the flow is
    # terminal. Steps already done stay done (their work happened).
    cancelled_steps = 0
    steps = list(state.get("steps") or [])
    for step in steps:
        if not isinstance(step, dict):
            continue
        if step_status(step) in (StepStatus.DONE.value, StepStatus.CANCELLED.value):
            continue
        step["status"] = str(StepStatus.CANCELLED)
        step["cancel_reason"] = reason or "flow cancelled"
        cancelled_steps += 1
    if steps:
        state["steps"] = steps

    try:
        updated = await store_sqlite.update_flow(
            flow_id,
            revision,
            session_id=session_id,
            state=state,
            wait=None,
            status=TaskFlowStatus.CANCELLED.value,
        )
    except FlowConflictError as exc:
        return conflict_error(exc)
    except FlowNotFoundError:
        return not_found_error(flow_id)

    await push_taskflow_progress(session_id)
    return (
        f"TaskFlow cancelled: flow_id={flow_id}, revision={updated['expected_revision']}, "
        f"status={updated['status']}, steps_cancelled={cancelled_steps}"
    )
