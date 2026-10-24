"""taskflow_create: create a durable task flow (openclaw createManaged)."""

import time

from langchain_core.tools import tool

from config.features import TASKFLOW_INFRA

from ..registry import store_sqlite
from ..progress_push import push_taskflow_progress
from ..registry.store_sqlite import FlowExistsError
from ._shared import (
    bind_flow_todo,
    compensate_flow_without_todo,
    default_state,
    is_terminal,
    requester_session_key,
)
from agent.tools.pub_base import SessionId


@tool("taskflow_create")
async def taskflow_create(
    flow_id: str,
    description: str = "",
    initial_state: dict | None = None,
    session_id: SessionId = "",
    deadline_hours: float | None = None,
    resumed_from: str | None = None,
    todo_content: str = "",
    todo_category: str = "quick",
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

    A pending todo is created with the flow (``todo_content``, falling back to
    the description). That todo is the flow's GC root: it keeps the flow alive
    until the flow itself is terminal, and ``todowrite`` should carry it
    forward. Without ``todo_content`` the description (or flow id) is used, so
    a flow can never exist without a root.
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
            f"{TASKFLOW_INFRA['max_steps_per_flow']}. Use taskflow_flow(action='plan') to decompose "
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
        if is_terminal(existing["status"]):
            return (
                f"Error: TaskFlow '{flow_id}' already exists and is terminal "
                f"(status={existing['status']}); pass a different flow_id, or create "
                f"the successor with resumed_from='{flow_id}'."
            )
        return (
            f"TaskFlow already exists (idempotent): flow_id={flow_id}, "
            f"revision={existing['expected_revision']}, status={existing['status']}; "
            "nothing new was created. Re-read it with taskflow_inspect(action='summary'), or continue "
            "it with taskflow_step(action='resume')."
        )

    # Entry defense: the flow's GC root. The flow and todo live in separate
    # SQLite files, so this is a compensating write, not a shared transaction;
    # a failed bind fails the flow rather than leaving an unreferenced orphan.
    todo_text = (todo_content or description or f"TaskFlow {flow_id}").strip()
    try:
        await bind_flow_todo(
            session_id,
            flow_id,
            todo_text,
            category=todo_category,
        )
    except Exception as exc:  # noqa: BLE001 - report the compensated outcome
        await compensate_flow_without_todo(flow)
        return (
            f"Error: TaskFlow '{flow_id}' was created but its todo could not be "
            f"bound ({type(exc).__name__}: {exc}); the flow was failed so it cannot "
            "outlive its root. Retry the create."
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
        f"revision={flow['expected_revision']}{deadline_text}{lineage_text}; "
        f"todo bound: {todo_text}"
    )
