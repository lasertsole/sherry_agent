"""Compile verified TaskFlow plan knowledge into LDW workflow metadata."""

from __future__ import annotations

import json
from typing import Any

from pydantic import BaseModel

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


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def render_step_result(value: Any, *, structured: bool) -> str:
    """Render one step result deterministically for the journal prompt.

    A schema-bound step's result is canonical JSON (the same canonical form the
    LDW engine journals); a schema-less step's result is plain text. Both the
    LDW orchestration prompt and the TaskFlow write-back use this helper, so a
    degraded run writes the exact key the next LDW run will look up.
    """
    if isinstance(value, BaseModel):
        return _canonical_json(value.model_dump(mode="json", by_alias=True))
    if structured:
        if isinstance(value, str):
            try:
                return _canonical_json(json.loads(value))
            except (TypeError, ValueError):
                return value
        if isinstance(value, (dict, list)):
            return _canonical_json(value)
    return str(value)


def render_dependency_results(
    deps: dict[str, Any],
    order: list[str],
    structured_ids: set[str],
) -> str:
    """Render previous-step results in dependency order, one labelled block each."""
    lines: list[str] = []
    for dep_id in order:
        if dep_id not in deps:
            continue
        lines.append(f"[{dep_id}]")
        lines.append(render_step_result(deps[dep_id], structured=dep_id in structured_ids))
    return "\n".join(lines)


def _step_contract(step: dict[str, Any]) -> dict[str, Any]:
    """Copy the optional TaskFlow step fields this compiler preserves."""
    optional = (
        "response_schema",
        "step_model",
        "judge_criteria",
        "validation_criteria",
        "step_timeout_seconds",
        "retry_policy",
        "skills",
        "expected_params",
        "input_bindings",
        "priority",
        "file_deps",
    )
    out = {key: step[key] for key in optional if step.get(key) is not None}
    if step.get("aggregate_deps"):
        out["aggregate_deps"] = True
    return out


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
                "hard_constraints": [
                    str(item)
                    for item in (
                        step.get("hard_constraints") or plan_knowledge.get("hard_constraints") or []
                    )
                    if str(item).strip()
                ],
                **_step_contract(step),
            }
        )
    if not compiled:
        raise ValueError("plan knowledge produced no usable steps")
    return {
        "plan_key": str(plan_knowledge.get("plan_key") or ""),
        "plan_name": str(plan_knowledge.get("plan_name") or ""),
        "flow_id": str(plan_knowledge.get("flow_id") or ""),
        "steps": compiled,
    }


def structured_step_ids(workflow_meta: dict[str, Any]) -> set[str]:
    """Step ids whose result is schema-bound (canonical-JSON rendering)."""
    return {
        str(step.get("step_id"))
        for step in (workflow_meta.get("steps") or [])
        if isinstance(step, dict) and isinstance(step.get("response_schema"), dict)
    }


def build_step_prompt(
    step: dict[str, Any],
    deps: dict[str, Any],
    variant_input: str,
    structured_ids: set[str],
) -> str:
    """One leaf prompt; the single source shared with the TaskFlow adapter."""
    prompt = f"{step.get('task_template') or ''}\n[Current task input]\n{variant_input}"
    constraints = step.get("hard_constraints") or []
    if constraints:
        prompt += "\n[Hard constraints]\n" + "\n".join(f"- {item}" for item in constraints)
    prompt += "\n[Previous step results]\n" + render_dependency_results(
        deps, list(step.get("deps") or []), structured_ids
    )
    return prompt


def build_orchestrate(workflow_meta: dict[str, Any], variant_input: str):
    """Build the async LDW orchestration callable for one variant input."""
    steps = workflow_meta.get("steps") or []
    if not steps:
        raise ValueError("workflow metadata has no steps")
    structured_ids = structured_step_ids(workflow_meta)

    async def orchestrate(ctx):
        vi = str(variant_input or "")
        nodes = []
        for step in steps:
            schema = step.get("response_schema")
            schema_arg = schema if isinstance(schema, dict) else None

            def make_run(s=step, sch=schema_arg):
                async def run(deps):
                    prompt = build_step_prompt(s, deps, vi, structured_ids)
                    kwargs: dict[str, Any] = {"prompt": prompt, "agent_type": s["agent_type"]}
                    if sch is not None:
                        kwargs["schema"] = sch
                    return await ctx.agent(**kwargs)

                return run

            nodes.append(
                DagNode(
                    id=step["step_id"],
                    deps=list(step.get("deps") or []),
                    run=make_run(),
                )
            )
        return await ctx.dag(nodes)

    return orchestrate
