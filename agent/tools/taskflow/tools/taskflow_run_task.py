"""taskflow_run_task: register a step and dispatch a detached child subagent
(openclaw managedFlows runTask).

The dispatch goes through the EXISTING spawn entry point
(spawn_subagent_direct); the child session completes via the existing
announce / settle-wake pipeline, which delivers the result back to the
requester session automatically. No taskflow-local callback is created. The
dispatch lives in the shared ``_dispatch.py`` module and is called
MODULE-QUALIFIED so tests and alternative runtimes can substitute the seam
without touching the spawn pipeline.
"""

import time
from typing import Annotated

from langchain_core.tools import tool
from langgraph.prebuilt.tool_node import InjectedState

from ..config import StepStatus
from ..registry import store_sqlite
from ..registry.store_sqlite import FlowConflictError, FlowNotFoundError
from . import _dispatch
from ._shared import (
    conflict_error,
    deps_satisfied,
    is_terminal,
    new_step,
    not_found_error,
    requester_session_key,
    step_status,
    terminal_error,
)

SessionId = Annotated[str, InjectedState("session_id")]


@tool("taskflow_run_task")
async def taskflow_run_task(
    flow_id: str,
    task: str,
    label: str | None = None,
    expected_revision: int | None = None,
    depends_on: list[str] | None = None,
    session_id: SessionId = "",
) -> str:
    """Register a step on the flow and dispatch it to a detached child subagent.

    The child session runs independently; its result is delivered back
    automatically via the announce pipeline. Inject it into the flow state
    afterwards with taskflow_resume. Pass expected_revision to fail fast on
    concurrent writers.

    Pass depends_on to declare prerequisite step ids. When any prerequisite is
    not yet ``done`` the step is registered as ``blocked`` and NOT dispatched
    (no child is spawned); resume/dispatch unlock it later. An unknown
    dependency id is rejected before any spawn or state change.
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

    state = dict(flow["state"])
    steps = list(state.get("steps") or [])
    deps = list(depends_on or [])

    existing_ids = {step.get("step_id") for step in steps}
    unknown_deps = [dep for dep in deps if dep not in existing_ids]
    if unknown_deps:
        return f"Error: unknown depends_on step id(s): {', '.join(unknown_deps)}"

    revision = (
        int(expected_revision) if expected_revision is not None else flow["expected_revision"]
    )
    requester_key = requester_session_key(session_id)
    step_id = f"step-{len(steps) + 1}"

    candidate = new_step(step_id, task, depends_on=deps, status=StepStatus.READY)
    if not deps_satisfied(candidate, steps):
        candidate["status"] = str(StepStatus.BLOCKED)
        steps.append(candidate)
        state["steps"] = steps

        try:
            blocked = await store_sqlite.update_flow(flow_id, revision, state=state)
        except FlowConflictError as exc:
            return conflict_error(exc)
        except FlowNotFoundError:
            return not_found_error(flow_id)

        by_id = {step.get("step_id"): step for step in steps}
        pending = [dep for dep in deps if step_status(by_id[dep]) != StepStatus.DONE]
        return (
            f"TaskFlow step registered (blocked): flow_id={flow_id}, step_id={step_id}, "
            f"pending=[{','.join(pending)}], revision={blocked['expected_revision']}"
        )

    child_session_key = await _dispatch.dispatch_child(
        task=task, requester_session_key=requester_key, label=label
    )

    dispatched = new_step(step_id, task, depends_on=deps, status=StepStatus.DISPATCHED)
    dispatched["child_session_key"] = child_session_key
    dispatched["dispatched_at"] = time.time()
    steps.append(dispatched)
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
