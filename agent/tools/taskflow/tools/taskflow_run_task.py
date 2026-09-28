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
from typing import Annotated, Any

from langchain_core.tools import tool
from langgraph.prebuilt.tool_node import InjectedState

from ..config import StepStatus
from ..registry import store_sqlite
from ..registry.store_sqlite import FlowConflictError, FlowNotFoundError
from . import _dispatch
from ._retry import validate_policy
from ._shared import (
    build_task_with_bindings,
    build_task_with_dep_results,
    conflict_error,
    deps_satisfied,
    is_terminal,
    new_step,
    not_found_error,
    requester_session_key,
    step_status,
    terminal_error,
    update_flow_with_conflict_retry,
)

SessionId = Annotated[str, InjectedState("session_id")]


@tool("taskflow_run_task")
async def taskflow_run_task(
    flow_id: str,
    task: str,
    label: str | None = None,
    expected_revision: int | None = None,
    depends_on: list[str] | None = None,
    validation_criteria: str | None = None,
    retry_policy: dict | None = None,
    aggregate_deps: bool = False,
    response_schema: dict | None = None,
    expected_params: dict | None = None,
    input_bindings: dict | None = None,
    judge_criteria: str | None = None,
    judge_model: str | None = None,
    functional_role: str | None = None,
    step_model: str | None = None,
    step_timeout_seconds: float | None = None,
    priority: int | None = None,
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

    Pass validation_criteria (natural-language acceptance criteria) to store
    them on the step; taskflow_resume echoes them with the child result so the
    orchestrator can judge whether the result passes. The tool itself never
    enforces the criteria.

    Pass retry_policy={"max_retries": N, "retry_delay_seconds": S,
    "retry_on": ["timeout", ...]} to let a failed child be re-dispatched
    automatically: taskflow_wait_all retries a settled (dead) child and
    taskflow_resume retries a result whose text classifies as a failure in
    retry_on (empty list = every classified failure). Repairs stop once
    retry_count reaches max_retries.

    Pass aggregate_deps=True on a synthesis step to append its dependency
    steps' recorded results to the dispatched task text. The flag is stored on
    the step, so a later batch dispatch or retry re-derives the aggregation
    from the (stable) dependency results without rewriting the stored task.

    Expectation side of the closure (all optional, stored on the step and
    enforced by taskflow_resume):

    * response_schema — JSON Schema the child's structured output must satisfy.
      It is passed to the spawned sub-agent as its output contract, and the
      result is schema-validated on resume (Tier 1).
    * judge_criteria / judge_model — semantic acceptance criteria judged by the
      auxiliary-LLM step judge (Tier 2), and an optional model override.
    * expected_params — structured inputs the step is supposed to work on
      (recorded for the caller/UI; nothing is enforced from it).
    * input_bindings — {"param": "step-A.structured_result.files"} mappings read
      from upstream steps' structured results and appended to this step's
      dispatched task text.

    Execution metadata: functional_role (sub-agent specialization), step_model
    (child model override), step_timeout_seconds (per-step child timeout),
    priority (dispatch ordering hint).
    """
    flow_id = (flow_id or "").strip()
    task = (task or "").strip()
    criteria = (validation_criteria or "").strip()
    if not flow_id:
        return "Error: flow_id is required"
    if not task:
        return "Error: task is required"
    policy_error = validate_policy(retry_policy)
    if policy_error:
        return policy_error

    flow = await store_sqlite.get_flow(flow_id, session_id)
    if flow is None:
        return not_found_error(flow_id)
    if is_terminal(flow["status"]):
        return terminal_error(flow_id, flow["status"])

    state = dict(flow["state"])
    steps = list(state.get("steps") or [])
    results = list(state.get("results") or [])
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

    optional_fields: dict[str, Any] = {
        "validation_criteria": criteria or None,
        "retry_policy": retry_policy,
        "response_schema": response_schema,
        "expected_params": expected_params,
        "input_bindings": input_bindings,
        "judge_criteria": (judge_criteria or "").strip() or None,
        "judge_model": (judge_model or "").strip() or None,
        "functional_role": (functional_role or "").strip() or None,
        "step_model": (step_model or "").strip() or None,
        "step_timeout_seconds": step_timeout_seconds,
        "priority": priority,
    }
    candidate = new_step(step_id, task, depends_on=deps, status=StepStatus.READY, **optional_fields)
    if aggregate_deps:
        candidate["aggregate_deps"] = True
    if not deps_satisfied(candidate, steps):
        candidate["status"] = str(StepStatus.BLOCKED)
        steps.append(candidate)
        state["steps"] = steps

        try:
            blocked = await store_sqlite.update_flow(
                flow_id, revision, session_id=session_id, state=state
            )
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
        task=build_task_with_bindings(
            build_task_with_dep_results(candidate, steps, results), candidate, steps, results
        ),
        requester_session_key=requester_key,
        label=label,
        output_schema=response_schema,
        functional_role=optional_fields["functional_role"],
        step_model=optional_fields["step_model"],
        step_timeout_seconds=step_timeout_seconds,
    )
    dispatched_at = time.time()

    def build_state(fresh_flow: dict, _attempt: int) -> dict:
        nonlocal step_id
        fresh_state = dict(fresh_flow["state"])
        fresh_steps = list(fresh_state.get("steps") or [])
        step_id = f"step-{len(fresh_steps) + 1}"
        dispatched = new_step(
            step_id, task, depends_on=deps, status=StepStatus.DISPATCHED, **optional_fields
        )
        if aggregate_deps:
            dispatched["aggregate_deps"] = True
        dispatched["child_session_key"] = child_session_key
        dispatched["dispatched_at"] = dispatched_at
        fresh_steps.append(dispatched)
        fresh_state["steps"] = fresh_steps
        return fresh_state

    updated, error = await update_flow_with_conflict_retry(
        flow_id,
        revision,
        flow,
        build_state,
        child_keys=[child_session_key],
        session_id=session_id,
        flow_child_session_key=child_session_key,
    )
    if updated is None:
        return error

    return (
        f"TaskFlow step dispatched: flow_id={flow_id}, step_id={step_id}, "
        f"child_session_key={child_session_key}, revision={updated['expected_revision']}"
    )
