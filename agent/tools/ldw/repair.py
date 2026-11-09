"""Knowledge merge + repair extraction for LDW replay failures."""

from __future__ import annotations

import json
from typing import Any

from loguru import logger


def _dedupe(items: list[Any]) -> list[Any]:
    seen: set[str] = set()
    out: list[Any] = []
    for item in items:
        key = json.dumps(item, ensure_ascii=False, sort_keys=True, default=str)
        if key not in seen:
            seen.add(key)
            out.append(item)
    return out


def _merge_steps(original: list[dict], repaired: list[dict]) -> list[dict]:
    by_id = {str(step.get("step_id")): dict(step) for step in original if step.get("step_id")}
    for step in repaired:
        step_id = str(step.get("step_id") or "")
        if not step_id:
            continue
        previous = by_id.get(step_id, {})
        merged = dict(previous)
        merged.update(step)
        by_id[step_id] = merged
    return list(by_id.values())


def merge_knowledge(original: dict[str, Any], repaired: dict[str, Any]) -> dict[str, Any]:
    """Merge a repair result into plan knowledge.

    ``success_steps`` replaces (the repaired path was actually verified);
    failure branches / constraints / decisions are unioned and deduplicated.
    """
    merged = dict(original)
    if "steps" in original or "steps" in repaired:
        merged["steps"] = _merge_steps(
            list(original.get("steps") or []), list(repaired.get("steps") or [])
        )
    if "success_steps" in repaired:
        merged["success_steps"] = list(repaired.get("success_steps") or [])
    if "failed_steps" in repaired:
        merged["failed_steps"] = list(repaired.get("failed_steps") or [])
    for key in (
        "failed_branches",
        "hard_constraints",
        "locked_decisions",
        "discovered_hard_constraints",
    ):
        if key in original or key in repaired:
            merged[key] = _dedupe([*(original.get(key) or []), *(repaired.get(key) or [])])
    if repaired.get("variant_input"):
        merged["variant_input"] = str(repaired["variant_input"])
    merged["replay_count"] = max(
        int(original.get("replay_count") or 0), int(repaired.get("replay_count") or 0)
    )
    return merged


def extract_react_repair_experience(
    original: dict[str, Any],
    handoffs: list[dict[str, Any]],
    *,
    variant_input: str = "",
) -> dict[str, Any]:
    """Build the A-scenario repaired view from leaf handoffs."""
    success_steps: list[dict[str, Any]] = []
    failed_branches: list[dict[str, Any]] = []
    hard_constraints: list[str] = []
    locked_decisions: list[dict[str, Any]] = []
    for handoff in handoffs:
        if not isinstance(handoff, dict):
            continue
        success_steps.extend(
            item for item in (handoff.get("success_steps") or []) if isinstance(item, dict)
        )
        failed_branches.extend(
            item
            for item in (handoff.get("failed_branches") or handoff.get("failed_paths") or [])
            if isinstance(item, dict)
        )
        hard_constraints.extend(
            str(item) for item in (handoff.get("discovered_hard_constraints") or []) if str(item)
        )
        locked_decisions.extend(
            item for item in (handoff.get("locked_decisions") or []) if isinstance(item, dict)
        )
    repaired = {
        "success_steps": success_steps,
        "failed_branches": failed_branches,
        "hard_constraints": hard_constraints,
        "locked_decisions": locked_decisions,
        "variant_input": variant_input,
    }
    return merge_knowledge(original, repaired)


def extract_failure_experience(
    original: dict[str, Any],
    failed_steps: list[str],
    handoffs: list[dict[str, Any]],
    *,
    variant_input: str = "",
) -> dict[str, Any]:
    """Build the B-scenario merged view: failed branches + affected step ids."""
    failed_branches: list[dict[str, Any]] = []
    for handoff in handoffs:
        if not isinstance(handoff, dict):
            continue
        failed_branches.extend(
            item
            for item in (handoff.get("failed_branches") or handoff.get("failed_paths") or [])
            if isinstance(item, dict)
        )
    repaired = {
        "failed_branches": failed_branches,
        "failed_steps": list(failed_steps),
        "variant_input": variant_input,
    }
    return merge_knowledge(original, repaired)


async def recompile_ldw_workflow(plan_key: str, knowledge: dict[str, Any]):
    """Recompile the LDW skill script from repaired knowledge."""
    from .compiler import compile_plan_to_workflow
    from .scripts import write_ldw_script

    meta = compile_plan_to_workflow(knowledge)
    return write_ldw_script(plan_key, meta)


async def repair_extraction(
    plan_key: str,
    original: dict[str, Any],
    repaired: dict[str, Any],
    *,
    variant_input: str = "",
    plan_name: str = "",
) -> dict[str, Any]:
    """Merge, persist the success path, and recompile the LDW artifact.

    The caller owns the TaskFlow re-run; this function only closes the knowledge
    and artifact half of the repair loop.
    """
    merged = merge_knowledge(original, {**repaired, "variant_input": variant_input})
    try:
        from agent.tools.todolist.knowledge.identity import PlanIdentity
        from agent.tools.todolist.knowledge.knowledge_store import KnowledgeStore

        identity = PlanIdentity(
            key=plan_key,
            plan_name=plan_name or plan_key,
            plan_ref=None,
        )
        await KnowledgeStore.write(identity, layer="success", data=merged)
    except Exception as exc:
        # Persistence is best-effort; the in-memory merge still returns.
        logger.exception("LDW repair: failed to persist success knowledge for {}", plan_key)
        merged["persist_error"] = f"{type(exc).__name__}: {exc}"
    try:
        await recompile_ldw_workflow(plan_key, merged)
    except Exception as exc:  # noqa: BLE001 - repair is fail-open
        logger.exception("LDW repair: recompile failed for {}", plan_key)
        merged["recompile_error"] = f"{type(exc).__name__}: {exc}"
    return merged
