"""PTC replay-script compilation (job 5): one script per successful TaskFlow step, written after the skill judge accepts the update."""

import json
from typing import Any
from loguru import logger
from config.features import PTC
from runtime.lane import LaneType, lane_slot
from .skill_review import changed_skill_paths


def _review_skill_names(text: str) -> list[str]:
    """The skill names in the review's final JSON block (best-effort)."""
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end <= start:
        return []
    try:
        payload = json.loads(text[start : end + 1])
    except (TypeError, ValueError):
        return []
    if not isinstance(payload, dict):
        return []
    return [str(name) for name in payload.get("skills") or [] if str(name).strip()]


def _owning_skill_after_review(
    before: Any,
    after: Any,
    review_text: str,
) -> tuple[str, str] | None:
    """Pick the skill that owns this plan's PTC scripts.

    Prefer the skill the review says it updated; fall back to the first changed
    SKILL.md. Returns ``(leaf_skill_name, SKILL.md text)`` or ``None``.
    """
    changed = changed_skill_paths(before, after)
    skill_mds = sorted(
        path
        for path in changed
        if path.endswith("SKILL.md") and path in getattr(after, "files", {})
    )
    if not skill_mds:
        return None

    preferred = _review_skill_names(review_text)
    selected = None
    for name in preferred:
        leaf = name.rsplit("/", 1)[-1]
        for rel in skill_mds:
            parent = rel.rsplit("/", 1)[0]
            if parent.rsplit("/", 1)[-1] == leaf:
                selected = rel
                break
        if selected:
            break
    if selected is None:
        selected = skill_mds[0]
    parent = selected.rsplit("/", 1)[0]
    if not parent:
        return None
    try:
        content = after.files[selected].decode("utf-8")
    except (KeyError, UnicodeDecodeError):
        return None
    return parent.rsplit("/", 1)[-1], content


def _step_task(context: dict[str, Any], step_id: str, fallback: str) -> str:
    for todo in context.get("todos") or []:
        if str(todo.get("step_id") or "") == step_id:
            text = str(todo.get("content") or "").strip()
            if text:
                return text
    for run in context.get("subagent_runs") or []:
        if str(run.get("step_id") or "") == step_id:
            text = str(run.get("task") or "").strip()
            if text:
                return text
    return fallback[:4_000]


def _ptc_tool_signatures(allowed_tools: set[str]) -> list[str]:
    from agent.tools import build_main_tools
    from agent.tools.ptc.stub_generator import tool_specs_from_base_tools

    by_name = {getattr(tool, "name", ""): tool for tool in build_main_tools()}
    tools = [by_name[name] for name in sorted(allowed_tools) if name in by_name]
    rendered: list[str] = []
    for spec in tool_specs_from_base_tools(tools):
        params = ", ".join(
            name if default is None else f"{name}={default}" for name, default in spec.params
        )
        rendered.append(f"{spec.name}({params})")
    return rendered


async def _compile_ptc_scripts(
    context: dict[str, Any],
    before: Any,
    after: Any,
    review_text: str,
) -> None:
    """Best-effort: compile one replay script per successful TaskFlow step.

    Runs AFTER the skill judge has accepted the skill update, so a compile miss
    never rolls the skill back. The compiler returns source; this function
    validates the filename and writes it through ``skill_manage``.
    """
    from agent.tools.ptc.compiler import compile_step_script
    from agent.tools.ptc.scripts import script_filename

    if not PTC["ptc_prefill_enabled"]:
        return
    plan_key = str(context.get("plan_key") or "").strip()
    if not plan_key:
        logger.debug("PTC compile: no plan_key, skipping")
        return
    owning = _owning_skill_after_review(before, after, review_text)
    if owning is None:
        logger.debug("PTC compile: no changed SKILL.md, skipping")
        return
    skill_name, skill_excerpt = owning

    aggregated = context.get("aggregated_handoff") or {}
    groups: dict[str, list[dict[str, Any]]] = {}
    for step in aggregated.get("success_steps") or []:
        if not isinstance(step, dict):
            continue
        step_id = str(step.get("step_id") or "").strip()
        if step_id and str(step.get("tool") or "").strip():
            groups.setdefault(step_id, []).append(step)
    if not groups:
        logger.debug("PTC compile: no step-attributed success_steps, skipping")
        return

    plan_name = str(context.get("plan_name") or "")
    hard_constraints = [
        str(item) for item in (aggregated.get("hard_constraints") or []) if str(item).strip()
    ]
    allowed_ptc = set(PTC["ptc_allowed_tools"])
    from agent.tools.skill_tools.skill_manage import _write_file

    # skill_manage resolves a skill by its leaf name. Two categories may hold
    # the same leaf; writing through that API could hit the wrong skill.
    from config import AUTO_SKILLS_DIR

    same_name = [
        path.parent
        for path in AUTO_SKILLS_DIR.glob("**/SKILL.md")
        if path.parent.name == skill_name
    ]
    if len(same_name) > 1:
        logger.warning(
            "PTC compile: owning skill name {!r} is ambiguous ({}); skipping",
            skill_name,
            [str(path) for path in same_name],
        )
        return

    for step_id, steps in sorted(groups.items()):
        filename = script_filename(plan_key, step_id)
        if filename is None:
            logger.debug("PTC compile: unsafe step id {!r}, skipping", step_id)
            continue
        allowed = {
            str(step.get("tool") or "").strip()
            for step in steps
            if str(step.get("tool") or "").strip() in allowed_ptc
        }
        if not allowed:
            logger.debug("PTC compile: step {} has no PTC-allowed tool, skipping", step_id)
            continue
        flow_id = next(
            (str(step.get("flow_id") or "") for step in steps if step.get("flow_id")), ""
        )
        task = _step_task(context, step_id, str(steps[0].get("step") or ""))
        async with lane_slot(LaneType.BACKGROUND_REVIEW):
            code = await compile_step_script(
                plan_key=plan_key,
                plan_name=plan_name,
                step_id=step_id,
                flow_id=flow_id or None,
                task=task,
                success_steps=steps,
                hard_constraints=hard_constraints,
                skill_excerpt=skill_excerpt,
                tool_signatures=_ptc_tool_signatures(allowed),
                allowed_tools=allowed,
            )
        if not code:
            continue
        result = _write_file(skill_name, f"scripts/{filename}", code)
        if not result.get("success"):
            logger.warning(
                "PTC compile: cannot write {} into {}: {}",
                filename,
                skill_name,
                result.get("error"),
            )
        else:
            logger.info("PTC compile: wrote {}/scripts/{}", skill_name, filename)
