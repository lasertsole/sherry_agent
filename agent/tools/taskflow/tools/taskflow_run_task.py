"""taskflow_run_task: register a step and dispatch a detached child subagent
(openclaw managedFlows runTask).

The dispatch goes through the EXISTING spawn entry point
(spawn_subagent_direct); the child session completes via the existing
announce / settle-wake pipeline, which delivers the result back to the
requester session automatically. No taskflow-local callback is created. The
dispatch function is a module-level injectable reference (``_dispatch_child``)
so tests and alternative runtimes can substitute it via monkeypatch without
touching the spawn pipeline.
"""

import time
from typing import Annotated

from langchain_core.tools import tool
from langgraph.prebuilt.tool_node import InjectedState

from ..registry import store_sqlite
from ..registry.store_sqlite import FlowConflictError, FlowNotFoundError
from ._shared import (
    conflict_error,
    is_terminal,
    not_found_error,
    requester_session_key,
    terminal_error,
)

SessionId = Annotated[str, InjectedState("session_id")]


async def _default_dispatch_child(
    task: str, requester_session_key: str, label: str | None = None
) -> str:
    """Dispatch a detached child session via the existing spawn entry point."""
    from agent.tools.subagent import spawn_subagent_direct

    result = await spawn_subagent_direct(
        task=task,
        requester_session_key=requester_session_key,
        label=label,
        expects_completion_message=True,
    )
    if result.status != "accepted" or not result.child_session_key:
        raise RuntimeError(f"status={result.status} error={result.error}")
    return result.child_session_key


# Module-level injectable dispatch reference: monkeypatch this seam in tests.
_dispatch_child = _default_dispatch_child


@tool("taskflow_run_task")
async def taskflow_run_task(
    flow_id: str,
    task: str,
    label: str | None = None,
    expected_revision: int | None = None,
    session_id: SessionId = "",
) -> str:
    """Register a step on the flow and dispatch it to a detached child subagent.

    The child session runs independently; its result is delivered back
    automatically via the announce pipeline. Inject it into the flow state
    afterwards with taskflow_resume. Pass expected_revision to fail fast on
    concurrent writers.
    """
    flow_id = (flow_id or "").strip()
    task = (task or "").strip()
    if not flow_id:
        return "Error: flow_id is required"
    if not task:
        return "Error: task is required"

    flow = await store_sqlite.get_flow(flow_id)
    if flow is None:
        return not_found_error(flow_id)
    if is_terminal(flow["status"]):
        return terminal_error(flow_id, flow["status"])

    revision = (
        int(expected_revision) if expected_revision is not None else flow["expected_revision"]
    )
    requester_key = requester_session_key(session_id)

    child_session_key = await _dispatch_child(
        task=task, requester_session_key=requester_key, label=label
    )

    state = dict(flow["state"])
    steps = list(state.get("steps") or [])
    step_id = f"step-{len(steps) + 1}"
    steps.append(
        {
            "step_id": step_id,
            "task": task,
            "child_session_key": child_session_key,
            "dispatched_at": time.time(),
        }
    )
    state["steps"] = steps

    try:
        updated = await store_sqlite.update_flow(
            flow_id,
            revision,
            state=state,
            child_session_key=child_session_key,
        )
    except FlowConflictError as exc:
        return conflict_error(exc)
    except FlowNotFoundError:
        return not_found_error(flow_id)

    return (
        f"TaskFlow step dispatched: flow_id={flow_id}, step_id={step_id}, "
        f"child_session_key={child_session_key}, revision={updated['expected_revision']}"
    )
