"""``taskflow_replan`` — regenerate a flow's remaining steps after a failure.

``taskflow_update_steps`` can already replace a DAG, but the model has to author
the whole step list by hand (and re-state the DAG it only half remembers). This
tool turns that into one call: it reads the flow, keeps every ``done``/
``dispatched`` step untouched, asks an auxiliary LLM (temperature 0) for
replacement steps for the rest, and applies the result through the same update
path — so the six safety rules and the optimistic lock still run.

Transitive invalidation follows senpi-task's ``amendRun`` idea: steps that
(directly or transitively) depend on a step whose task changed are marked
``blocked`` again, because their inputs moved. They unlock through the normal
dependency pass once the new step completes.

The replan loop is bounded: every call increments ``replan_count`` on the flow
state, and past :data:`REPLAN_MAX` the tool refuses and points at
``taskflow_fail`` instead of regenerating forever.
"""

from __future__ import annotations

import json
from typing import Any

from langchain_core.tools import tool
from loguru import logger

from agent.tools.pub_base import SessionId

from ..config import StepStatus, TaskFlowStatus
from ..registry import store_sqlite
from ._shared import (
    is_terminal,
    new_step,
    not_found_error,
    step_status,
    steps_summary,
    terminal_error,
    update_flow_with_conflict_retry,
)

#: How many replans one flow may spend before the tool points at taskflow_fail.
REPLAN_MAX = 3

#: Characters of a step result handed to the planner (per step).
_RESULT_CLIP = 1200

_SYSTEM_PROMPT = """You re-plan the remaining steps of a task flow after a failure.

You are given the original goal, the steps that are already DONE (with their \
results — treat those as fixed facts), and the steps that still need work \
(blocked/ready/failed), including the failure that triggered this replan.

Return ONLY a JSON array of the new steps that should replace the non-done \
part, in dependency order:

[{"step_id": "step-3", "task": "...", "depends_on": ["step-1"], \
"judge_criteria": "optional acceptance criteria", "response_schema": {optional JSON schema}}]

Rules:
* ``step_id`` must be unique. Reuse the id of a step you are replacing when it \
still means the same thing.
* ``depends_on`` may only reference DONE steps or steps you are defining here. \
Never reference an in-flight (dispatched) step.
* Keep the non-done list SHORT — only the steps actually needed to finish.
* No prose, no markdown fence: the JSON array only.
"""


def _clip(text: Any, limit: int = _RESULT_CLIP) -> str:
    value = text if isinstance(text, str) else json.dumps(text, ensure_ascii=False, default=str)
    return value if len(value) <= limit else value[: limit - 1] + "…"


def _transitive_dependents(steps: list[dict], changed_ids: set[str]) -> set[str]:
    """Every step that directly or transitively depends on ``changed_ids``.

    Borrowed from senpi-task's ``amendRun``: a step whose task changed
    invalidates the inputs of everything downstream of it, so those steps go
    back to ``blocked`` instead of running on stale data. One BFS per level, so
    a cycle terminates instead of looping.
    """
    invalidated: set[str] = set()
    frontier = set(changed_ids)
    while frontier:
        next_frontier: set[str] = set()
        for step in steps:
            step_id = str(step.get("step_id") or "")
            if not step_id or step_id in invalidated or step_id in frontier:
                continue
            deps = {str(dep) for dep in (step.get("depends_on") or [])}
            if deps & frontier:
                next_frontier.add(step_id)
        invalidated |= next_frontier
        frontier = next_frontier
    return invalidated


def _build_replan_prompt(flow: dict, reason: str, blocked_step_id: str | None) -> str:
    state = dict(flow.get("state") or {})
    steps = list(state.get("steps") or [])
    lines: list[str] = [f"Goal: {state.get('description') or flow.get('description') or '(none)'}"]
    done = [s for s in steps if step_status(s) == StepStatus.DONE]
    others = [s for s in steps if step_status(s) != StepStatus.DONE]
    lines.append("\nDONE steps (fixed; keep their meaning):")
    if not done:
        lines.append("  (none)")
    for step in done:
        lines.append(
            f"  [{step.get('step_id')}] {step.get('task')}\n"
            f"    result: {_clip(_result_for(state, step))}"
        )
    lines.append("\nSteps still needing work:")
    for step in others:
        lines.append(
            f"  [{step.get('step_id')}] status={step_status(step)} task={step.get('task')} "
            f"depends_on={step.get('depends_on') or []}"
            + (
                f"\n    last result: {_clip(_result_for(state, step))}"
                if _result_for(state, step)
                else ""
            )
        )
    lines.append(f"\nReason for replanning: {reason or '(unspecified)'}")
    if blocked_step_id:
        lines.append(f"Triggering step: {blocked_step_id}")
    lines.append("\nReturn the replacement JSON array for the non-done steps.")
    return "\n".join(lines)


def _result_for(state: dict, step: dict) -> str:
    """The recorded result for a step's child, when one was injected."""
    key = str(step.get("child_session_key") or "")
    for record in state.get("results") or []:
        if isinstance(record, dict) and (
            record.get("child_session_key") == key or record.get("step_id") == step.get("step_id")
        ):
            return str(record.get("result") or "")
    return str(step.get("last_result") or "")


def _parse_steps(raw: str) -> list[dict]:
    """Parse the planner's JSON array (tolerating a fenced block)."""
    text = (raw or "").strip()
    if text.startswith("```"):
        text = text.strip("`")
        text = text.split("\n", 1)[1] if "\n" in text else text
    start, end = text.find("["), text.rfind("]")
    if start == -1 or end == -1 or end <= start:
        raise ValueError("planner returned no JSON array")
    parsed = json.loads(text[start : end + 1])
    if not isinstance(parsed, list):
        raise ValueError("planner returned a non-list")
    steps: list[dict] = []
    for entry in parsed:
        if not isinstance(entry, dict):
            continue
        step_id = str(entry.get("step_id") or "").strip()
        task = str(entry.get("task") or "").strip()
        if not step_id or not task:
            continue
        steps.append(entry)
    if not steps:
        raise ValueError("planner returned no usable steps")
    return steps


@tool("taskflow_replan")
async def taskflow_replan(
    flow_id: str,
    reason: str = "",
    blocked_step_id: str | None = None,
    keep_done: bool = True,
    session_id: SessionId = "",
) -> str:
    """Re-plan a flow's remaining steps after a step failed or its premise moved.

    Keeps every ``done`` (and, when ``keep_done`` is true, every completed) step
    exactly as it is, asks an auxiliary model for replacement steps for the rest,
    and applies them through the normal update path (DAG safety rules + the
    optimistic lock still run). Steps downstream of a step whose task changed go
    back to ``blocked`` so they cannot run on stale inputs, and the newly-ready
    steps are dispatched if ``auto_dispatch`` is on.

    Use it when the step judge BLOCKs, a step fails irrecoverably, or a result
    invalidates the assumptions of the steps that follow. Bounded per flow:
    after :data:`REPLAN_MAX` replans it refuses and points at ``taskflow_fail``.
    """
    return await _taskflow_replan_async(flow_id, reason, blocked_step_id, keep_done, session_id)


async def _taskflow_replan_async(
    flow_id: str,
    reason: str,
    blocked_step_id: str | None,
    keep_done: bool,
    session_id: str,
) -> str:
    flow_id = (flow_id or "").strip()
    if not flow_id:
        return "Error: flow_id is required"
    flow = await store_sqlite.get_flow(flow_id, session_id)
    if flow is None:
        return not_found_error(flow_id)
    if is_terminal(flow["status"]):
        return terminal_error(flow_id, flow["status"])

    state = dict(flow["state"] or {})
    replan_count = int(state.get("replan_count") or 0)
    if replan_count >= REPLAN_MAX:
        return (
            f"Error: flow_id '{flow_id}' already spent {replan_count} replan(s) "
            f"(max {REPLAN_MAX}); use taskflow_fail or a new flow"
        )

    steps = list(state.get("steps") or [])
    if not steps:
        return f"Error: flow_id '{flow_id}' has no steps to replan"

    prompt = _build_replan_prompt(flow, reason, blocked_step_id)
    from models import build_auxiliary_llm

    try:
        llm = build_auxiliary_llm(temperature=0)
        response = await llm.ainvoke(
            [
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": prompt},
            ]
        )
        raw = response.content if hasattr(response, "content") else str(response)
        planned = _parse_steps(str(raw))
    except Exception as exc:  # noqa: BLE001 - the tool boundary reports it as text
        logger.warning("taskflow_replan: planner failed for flow {}: {}", flow_id, exc)
        return f"Error: replan planning failed ({type(exc).__name__}: {exc})"

    kept = [s for s in steps if keep_done and step_status(s) == StepStatus.DONE]
    # The step set a dispatched child is still working on must survive: replacing
    # it would orphan the running child (a safety rule the update path enforces).
    in_flight = [s for s in steps if step_status(s) == StepStatus.DISPATCHED]
    kept_ids = {str(s.get("step_id")) for s in [*kept, *in_flight]}

    new_steps: list[dict] = []
    for entry in planned:
        step_id = str(entry.get("step_id")).strip()
        if step_id in kept_ids:
            continue
        deps = [str(dep) for dep in (entry.get("depends_on") or []) if str(dep).strip()]
        unknown = [
            dep
            for dep in deps
            if dep not in kept_ids and dep not in {str(e.get("step_id")).strip() for e in planned}
        ]
        if unknown:
            return f"Error: replanned step '{step_id}' depends on unknown step(s): {unknown}"
        step = new_step(
            step_id,
            str(entry.get("task")).strip(),
            depends_on=deps,
            status=StepStatus.BLOCKED if deps else StepStatus.READY,
            response_schema=entry.get("response_schema"),
            judge_criteria=entry.get("judge_criteria"),
        )
        new_steps.append(step)

    if not new_steps:
        return f"Error: replan produced no new steps for flow_id '{flow_id}'"

    rejected = [s for s in steps if step_status(s) not in (StepStatus.DONE, StepStatus.DISPATCHED)]
    changed_ids = {
        str(s.get("step_id"))
        for s in rejected
        if str(s.get("step_id")) in {str(n.get("step_id")) for n in new_steps}
    }
    invalidated = _transitive_dependents([*new_steps, *kept, *in_flight], changed_ids)
    for step in new_steps:
        step_id = str(step.get("step_id"))
        if step_id in invalidated and step_status(step) != StepStatus.READY:
            step["status"] = str(StepStatus.BLOCKED)
            step["block_reason"] = "replan: an upstream step's task changed"

    merged = [*kept, *in_flight, *new_steps]

    def build_state(fresh_flow: dict, _attempt: int) -> dict:
        fresh_state = dict(fresh_flow["state"] or {})
        fresh_state["steps"] = merged
        fresh_state["replan_count"] = replan_count + 1
        fresh_state["replan_reason"] = reason or ""
        return fresh_state

    updated, error = await update_flow_with_conflict_retry(
        flow_id,
        flow["expected_revision"],
        flow,
        build_state,
        # No spawn happens here: every child key belongs to a step the replan
        # KEPT (in-flight) rather than created, so the retry never has to
        # protect a fresh spawn.
        child_keys=[],
        session_id=session_id,
        update_kwargs={"status": TaskFlowStatus.RUNNING.value},
    )
    if updated is None:
        return error

    counts = steps_summary(merged)
    counts_text = ",".join(f"{status}={counts[status]}" for status in (s.value for s in StepStatus))
    replaced = [str(s.get("step_id")) for s in new_steps]
    logger.info(
        "taskflow_replan: flow={} replaced {} step(s) with {} (replan {}/{})",
        flow_id,
        [str(s.get("step_id")) for s in rejected],
        replaced,
        replan_count + 1,
        REPLAN_MAX,
    )
    return (
        f"TaskFlow replanned: flow_id={flow_id}, revision={updated['expected_revision']}, "
        f"replan={replan_count + 1}/{REPLAN_MAX}, new_steps=[{','.join(replaced)}], "
        f"kept=[{','.join(sorted(kept_ids))}], invalidated=[{','.join(sorted(invalidated))}], "
        f"step_statuses={counts_text}"
    )


__all__ = ["REPLAN_MAX", "taskflow_replan", "_transitive_dependents"]
