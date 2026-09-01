"""taskflow_create: create a durable task flow (openclaw createManaged)."""

from langchain_core.tools import tool

from ..config import INITIAL_REVISION
from ..registry import store_sqlite
from ..registry.store_sqlite import FlowExistsError
from ._shared import default_state


@tool("taskflow_create")
async def taskflow_create(
    flow_id: str,
    description: str = "",
    initial_state: dict | None = None,
) -> str:
    """Create a durable task flow and return its initial revision.

    A flow tracks multi-step work across turns with optimistic locking: every
    mutation bumps expected_revision, so concurrent writers are detected via
    revision conflicts instead of silent last-write-wins.
    """
    flow_id = (flow_id or "").strip()
    if not flow_id:
        return "Error: flow_id is required"

    state = default_state(description, initial_state)
    try:
        flow = await store_sqlite.create_flow(flow_id, state)
    except FlowExistsError:
        existing = await store_sqlite.get_flow(flow_id)
        revision = existing["expected_revision"] if existing else INITIAL_REVISION
        return (
            f"Error: TaskFlow '{flow_id}' already exists (revision={revision}). "
            "Re-read it with taskflow_summary."
        )
    return (
        f"TaskFlow created: flow_id={flow['flow_id']}, status={flow['status']}, "
        f"revision={flow['expected_revision']}"
    )
