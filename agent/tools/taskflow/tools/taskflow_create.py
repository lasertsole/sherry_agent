"""taskflow_create: create a durable task flow (openclaw createManaged)."""

import time
from typing import Annotated

from langchain_core.tools import tool
from langgraph.prebuilt.tool_node import InjectedState

from ..config import INITIAL_REVISION
from ..registry import store_sqlite
from ..registry.store_sqlite import FlowExistsError
from ._shared import default_state, requester_session_key

SessionId = Annotated[str, InjectedState("session_id")]


@tool("taskflow_create")
async def taskflow_create(
    flow_id: str,
    description: str = "",
    initial_state: dict | None = None,
    session_id: SessionId = "",
    deadline_hours: float | None = None,
) -> str:
    """Create a durable task flow and return its initial revision.

    A flow tracks multi-step work across turns with optimistic locking: every
    mutation bumps expected_revision, so concurrent writers are detected via
    revision conflicts instead of silent last-write-wins.

    Pass deadline_hours to set an overall deadline (e.g. 24.0 for 24 hours);
    the sweeper marks the flow as failed once the deadline passes.
    """
    flow_id = (flow_id or "").strip()
    if not flow_id:
        return "Error: flow_id is required"

    creator_key = requester_session_key(session_id) if session_id else ""
    state = default_state(description, initial_state, creator_session_key=creator_key)

    deadline_ts = None
    if deadline_hours is not None and deadline_hours > 0:
        deadline_ts = time.time() + (deadline_hours * 3600)

    try:
        flow = await store_sqlite.create_flow(flow_id, state, deadline_ts=deadline_ts)
    except FlowExistsError:
        existing = await store_sqlite.get_flow(flow_id)
        revision = existing["expected_revision"] if existing else INITIAL_REVISION
        return (
            f"Error: TaskFlow '{flow_id}' already exists (revision={revision}). "
            "Re-read it with taskflow_summary."
        )
    deadline_text = (
        f", deadline={time.strftime('%Y-%m-%d %H:%M', time.localtime(deadline_ts))}"
        if deadline_ts
        else ""
    )
    return (
        f"TaskFlow created: flow_id={flow['flow_id']}, status={flow['status']}, "
        f"revision={flow['expected_revision']}{deadline_text}"
    )
