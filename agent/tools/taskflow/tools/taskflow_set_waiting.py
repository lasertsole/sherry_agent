"""taskflow_set_waiting: park the flow in waiting state (openclaw setWaiting)."""

import time

from langchain_core.tools import tool

from ..config import TaskFlowStatus
from ..registry import store_sqlite
from ..registry.store_sqlite import FlowConflictError, FlowNotFoundError
from ._shared import conflict_error, is_terminal, not_found_error, terminal_error


@tool("taskflow_set_waiting")
async def taskflow_set_waiting(
    flow_id: str,
    wait_reason: str = "",
    expected_revision: int | None = None,
) -> str:
    """Park the flow in waiting state, recording what it is waiting for.

    Use when the flow cannot proceed until a child session result (or any
    external event) arrives. The wait payload is cleared again by
    taskflow_resume. Pass expected_revision to fail fast on concurrent writers.
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
    wait_payload = {"reason": wait_reason, "set_at": time.time()}

    try:
        updated = await store_sqlite.update_flow(
            flow_id,
            revision,
            wait=wait_payload,
            status=TaskFlowStatus.WAITING.value,
        )
    except FlowConflictError as exc:
        return conflict_error(exc)
    except FlowNotFoundError:
        return not_found_error(flow_id)

    return (
        f"TaskFlow waiting: flow_id={flow_id}, revision={updated['expected_revision']}, "
        f"status={updated['status']}"
    )
