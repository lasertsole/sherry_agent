"""taskflow_cancel: cancel the flow (openclaw requestCancel / cancel)."""

from langchain_core.tools import tool

from ..config import TaskFlowStatus
from ..registry import store_sqlite
from ..registry.store_sqlite import FlowConflictError, FlowNotFoundError
from ._shared import conflict_error, is_terminal, not_found_error, terminal_error


@tool("taskflow_cancel")
async def taskflow_cancel(
    flow_id: str,
    reason: str = "",
    expected_revision: int | None = None,
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

    flow = await store_sqlite.get_flow(flow_id)
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

    try:
        updated = await store_sqlite.update_flow(
            flow_id,
            revision,
            state=state,
            wait=None,
            status=TaskFlowStatus.CANCELLED.value,
        )
    except FlowConflictError as exc:
        return conflict_error(exc)
    except FlowNotFoundError:
        return not_found_error(flow_id)

    return (
        f"TaskFlow cancelled: flow_id={flow_id}, revision={updated['expected_revision']}, "
        f"status={updated['status']}"
    )
