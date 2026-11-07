"""Compile verified TaskFlow plan knowledge into LDW workflow metadata."""

from __future__ import annotations

import json
from typing import Any

from agent.tools.subagent.ldw_shim import DagNode


def agent_type_for_step(step: dict[str, Any]) -> str:
    """Stable leaf identity for one step.

    The role + extra-tool fingerprint keeps the journal partitioned by tool
    capability; the step suffix is required because the PTC prefill script is
    step-scoped and a shared runnable cannot know which step is executing.
    """
    role = str(step.get("functional_role") or "general")
    extra = sorted(str(name) for name in (step.get("extra_tools") or []) if str(name))
    suffix = f"+xt:{','.join(extra)}" if extra else ""
    return f"{role}{suffix}@{step.get('step_id') or '?'}"


def compile_plan_to_workflow(plan_knowledge: dict[str, Any]) -> dict[str, Any]:
    """Return JSON-persistable workflow metadata (no closures, no source)."""
    steps = plan_knowledge.get("steps") or []
    if not isinstance(steps, list) or not steps:
        raise ValueError("plan knowledge has no steps")
    compiled: list[dict[str, Any]] = []
    for step in steps:
        if not isinstance(step, dict):
            continue
        step_id = str(step.get("step_id") or "").strip()
        task = str(step.get("task") or step.get("task_template") or "").strip()
        if not step_id or not task:
            continue
        compiled.append(
            {
                "step_id": step_id,
                "task_template": task,
                "functional_role": str(step.get("functional_role") or "general"),
                "extra_tools": [str(n) for n in (step.get("extra_tools") or [])],
                "agent_type": agent_type_for_step(step),
                "deps": [str(dep) for dep in (step.get("depends_on") or step.get("deps") or [])],
                "response_schema": step.get("response_schema"),
                "step_model": step.get("step_model"),
                "hard_constraints": [
                    str(item)
                    for item in (
                        step.get("hard_constraints") or plan_knowledge.get("hard_constraints") or []
                    )
                    if str(item).strip()
                ],
            }
        )
    if not compiled:
        raise ValueError("plan knowledge produced no usable steps")
    return {
        "plan_key": str(plan_knowledge.get("plan_key") or ""),
        "flow_id": str(plan_knowledge.get("flow_id") or ""),
        "steps": compiled,
    }


def build_orchestrate(workflow_meta: dict[str, Any], variant_input: str):
    """Build the async LDW orchestration callable for one variant input."""
    steps = workflow_meta.get("steps") or []
    if not steps:
        raise ValueError("workflow metadata has no steps")

    async def orchestrate(ctx):
        vi = str(variant_input or "")
        nodes = []
        for step in steps:
            prompt = f"{step['task_template']}\n[Current task input]\n{vi}"
            constraints = step.get("hard_constraints") or []
            if constraints:
                prompt += "\n[Hard constraints]\n" + "\n".join(f"- {item}" for item in constraints)
            nodes.append(
                DagNode(
                    id=step["step_id"],
                    deps=list(step.get("deps") or []),
                    run=lambda deps, p=prompt, a=step["agent_type"]: ctx.agent(
                        prompt=f"{p}\n[Previous step results]\n{json.dumps(deps, ensure_ascii=False)}",
                        agent_type=a,
                    ),
                )
            )
        return await ctx.dag(nodes)

    return orchestrate
