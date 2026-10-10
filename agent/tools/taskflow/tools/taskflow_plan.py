"""``taskflow_plan`` — decompose a goal into a dependency-ordered flow.

The plan half of the loop closure: instead of hand-authoring a step list, the
model states the goal and this tool asks an auxiliary LLM (temperature 0) for a
dependency-ordered decomposition, creates the flow, and registers every step
BLOCKED/READY — nothing is dispatched. The model reviews (and may fix) the plan
with ``taskflow_update_steps`` and then starts it with ``taskflow_dispatch`` or
``taskflow_run_task``, so the plan is always seen before it spends tokens.
"""

from __future__ import annotations

import json

from langchain_core.tools import tool
from loguru import logger

from agent.tools.pub_base import SessionId

from ..config import StepStatus
from ..registry import store_sqlite
from ._shared import default_state, new_step, requester_session_key

#: Steps beyond this are dropped (the planner is told, but never trusted).
PLAN_MAX_STEPS = 12

_SYSTEM_PROMPT = """You decompose a goal into a dependency-ordered step list for a DAG executor.

Return ONLY a JSON array of steps, in an order where dependencies come first:

[{"step_id": "step-1", "task": "...", "depends_on": [], \
"judge_criteria": "optional acceptance criteria", "response_schema": {optional JSON schema}}]

Rules:
* ``step_id`` values are unique and stable ("step-1", "step-2", ...).
* ``depends_on`` may only reference steps defined EARLIER in your array. Never \
create a cycle.
* Each ``task`` is a self-contained instruction for a worker agent: what to do, \
what to produce. Include the concrete artefact or command when it is known.
* Add ``judge_criteria`` when a step's result can be checked objectively; add \
``response_schema`` when the step must return structured JSON.
* Prefer 3-8 steps. No prose, no markdown fence: the JSON array only.
"""


def _build_prompt(goal: str, context: str | None, max_steps: int) -> str:
    lines = [f"Goal: {goal}"]
    if context:
        lines.append(f"\nContext:\n{context}")
    lines.append(f"\nReturn at most {max_steps} steps.")
    return "\n".join(lines)


def _parse_plan(raw: str) -> list[dict]:
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
    plan: list[dict] = []
    seen: set[str] = set()
    for entry in parsed:
        if not isinstance(entry, dict):
            continue
        step_id = str(entry.get("step_id") or "").strip()
        task = str(entry.get("task") or "").strip()
        if not step_id or not task or step_id in seen:
            continue
        deps = [str(dep).strip() for dep in (entry.get("depends_on") or []) if str(dep).strip()]
        # A dependency on a step that does not exist yet would create a flow the
        # DAG can never unlock, so it is dropped rather than trusted.
        unknown = [dep for dep in deps if dep not in seen]
        if unknown:
            deps = [dep for dep in deps if dep in seen]
        seen.add(step_id)
        plan.append(
            {
                "step_id": step_id,
                "task": task,
                "depends_on": deps,
                "judge_criteria": entry.get("judge_criteria"),
                "response_schema": entry.get("response_schema"),
            }
        )
    if not plan:
        raise ValueError("planner returned no usable steps")
    return plan


@tool("taskflow_plan")
async def taskflow_plan(
    goal: str,
    flow_id: str = "",
    context: str | None = None,
    max_steps: int = 8,
    session_id: SessionId = "",
) -> str:
    """Decompose a goal into a dependency-ordered TaskFlow (nothing is started).

    Creates the flow, registers every planned step (blocked, or ready when it
    has no dependencies) and returns the plan for review. Start it afterwards
    with ``taskflow_dispatch`` (batch) or ``taskflow_run_task`` (reminder / full
    task text), or fix the plan first with ``taskflow_update_steps``.
    """
    goal = (goal or "").strip()
    if not goal:
        return "Error: goal is required"
    flow_id = (flow_id or "").strip()
    if not flow_id:
        flow_id = f"plan-{abs(hash(goal)) % 10**10}"
    limit = max(1, min(int(max_steps or 8), PLAN_MAX_STEPS))

    existing = await store_sqlite.get_flow(flow_id, session_id)
    if existing is not None:
        return (
            f"Error: flow_id '{flow_id}' already exists "
            f"(status={existing['status']}); pass another flow_id"
        )

    from models import build_auxiliary_llm

    try:
        llm = build_auxiliary_llm(temperature=0)
        response = await llm.ainvoke(
            [
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": _build_prompt(goal, context, limit)},
            ]
        )
        raw = response.content if hasattr(response, "content") else str(response)
        plan = _parse_plan(str(raw))[:limit]
    except Exception as exc:  # noqa: BLE001 - the tool boundary reports it as text
        logger.warning("taskflow_plan: planner failed for '{}': {}", goal[:60], exc)
        return f"Error: plan generation failed ({type(exc).__name__}: {exc})"

    steps = [
        new_step(
            entry["step_id"],
            entry["task"],
            depends_on=entry["depends_on"],
            status=StepStatus.BLOCKED if entry["depends_on"] else StepStatus.READY,
            response_schema=entry.get("response_schema"),
            judge_criteria=entry.get("judge_criteria"),
        )
        for entry in plan
    ]

    state = default_state(goal, None, creator_session_key=requester_session_key(session_id))
    state["steps"] = steps
    flow = await store_sqlite.create_flow(flow_id, state, session_id=session_id)

    lines = [
        f"TaskFlow planned: flow_id={flow_id}, revision={flow['expected_revision']}, "
        f"steps={len(steps)} (nothing dispatched)",
    ]
    for step in steps:
        deps = ",".join(step.get("depends_on") or []) or "-"
        gates: list[str] = []
        if step.get("judge_criteria"):
            gates.append("judge")
        if step.get("response_schema"):
            gates.append("schema")
        gate_text = f" gates={','.join(gates)}" if gates else ""
        lines.append(
            f"  [{step['step_id']}] ({step['status']}) deps={deps}{gate_text} {step['task']}"
        )
    lines.append(
        "Review the plan, then taskflow_dispatch(flow_id, [ids]) to start "
        "(or taskflow_update_steps to change it first)."
    )
    return "\n".join(lines)


__all__ = ["PLAN_MAX_STEPS", "taskflow_plan"]
