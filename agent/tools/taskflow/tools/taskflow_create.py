"""taskflow_create: create a durable task flow (openclaw createManaged)."""

import time

from langchain_core.tools import tool

from config.features import TASKFLOW_INFRA

from ..registry import store_sqlite
from ..progress_push import push_taskflow_progress
from ..registry.store_sqlite import FlowExistsError
from ._shared import default_state, requester_session_key
from agent.tools.pub_base import SessionId


@tool("taskflow_create")
async def taskflow_create(
    flow_id: str,
    description: str = "",
    initial_state: dict | None = None,
    session_id: SessionId = "",
    deadline_hours: float | None = None,
    resumed_from: str | None = None,
) -> str:
    """Create a durable task flow and return its initial revision.

    A flow tracks multi-step work across turns with optimistic locking: every
    mutation bumps expected_revision, so concurrent writers are detected via
    revision conflicts instead of silent last-write-wins.

    Pass deadline_hours to set an overall deadline (e.g. 24.0 for 24 hours);
    the sweeper marks the flow as failed once the deadline passes.

    Pass resumed_from=<predecessor flow_id> when this flow REPLACES an earlier
    one (e.g. the old flow went stale): the new flow records the lineage and the
    predecessor is stamped superseded — it drops out of the active-flow reads
    while remaining visible in the session board's history.
    """
    flow_id = (flow_id or "").strip()
    if not flow_id:
        return "Error: flow_id is required"
    session_id = (session_id or "").strip()
    if not session_id:
        return "Error: session_id is required"
    resumed_from = (resumed_from or "").strip() or None
    if resumed_from == flow_id:
        return f"Error: resumed_from cannot name the flow being created ('{flow_id}')"
    if resumed_from is not None:
        predecessor = await store_sqlite.get_flow(resumed_from, session_id)
        if predecessor is None:
            return (
                f"Error: resumed_from flow '{resumed_from}' not found in this session. "
                "Create without resumed_from, or name an existing flow_id."
            )

    creator_key = requester_session_key(session_id)
    state = default_state(description, initial_state, creator_session_key=creator_key)

    # Resource ceilings live at the tool layer (the store's contract is data
    # access, not policy): a hand-authored step avalanche is refused, and a
    # session holds a bounded number of OPEN flows — a finished or cancelled
    # flow releases its slot.
    step_count = len(state.get("steps") or [])
    if step_count > TASKFLOW_INFRA["max_steps_per_flow"]:
        return (
            f"Error: flow has {step_count} steps, max is "
            f"{TASKFLOW_INFRA['max_steps_per_flow']}. Use taskflow_plan to decompose "
            "the goal into fewer steps."
        )
    active_flows = store_sqlite.get_active_flows_sync(session_id)
    if len(active_flows) >= TASKFLOW_INFRA["max_flows_per_session"]:
        return (
            f"Error: session already has {len(active_flows)} active flows "
            f"(max {TASKFLOW_INFRA['max_flows_per_session']}). Finish or cancel existing "
            "flows before creating new ones."
        )

    deadline_ts = None
    if deadline_hours is not None and deadline_hours > 0:
        deadline_ts = time.time() + (deadline_hours * 3600)

    try:
        flow = await store_sqlite.create_flow(
            flow_id,
            state,
            session_id=session_id,
            deadline_ts=deadline_ts,
            resumed_from=resumed_from,
        )
    except FlowExistsError:
        existing = await store_sqlite.get_flow(flow_id, session_id)
        if existing is None:
            return f"Error: TaskFlow '{flow_id}' already exists; choose a different flow_id."
        revision = existing["expected_revision"]
        return (
            f"Error: TaskFlow '{flow_id}' already exists (revision={revision}). "
            "Re-read it with taskflow_summary."
        )
    lineage_text = ""
    if resumed_from is not None:
        marked = await store_sqlite.mark_flow_superseded(
            resumed_from, flow_id, session_id=session_id
        )
        lineage_text = f", resumed_from={resumed_from}"
        if not marked:
            # The predecessor vanished between the pre-check and the write; the
            # successor keeps its forward pointer, which is still true.
            lineage_text += " (predecessor not found when stamping superseded_by)"
    deadline_text = (
        f", deadline={time.strftime('%Y-%m-%d %H:%M', time.localtime(deadline_ts))}"
        if deadline_ts
        else ""
    )
    await push_taskflow_progress(session_id)
    return (
        f"TaskFlow created: flow_id={flow['flow_id']}, status={flow['status']}, "
        f"revision={flow['expected_revision']}{deadline_text}{lineage_text}"
    )
