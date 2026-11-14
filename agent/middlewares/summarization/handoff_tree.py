"""Handoff aggregation + plan context (job 3): read subagent runs and TaskFlow state, assemble/filter the tree, and build the compact review context."""

import json
from typing import Any
from loguru import logger
from config.features import BACKGROUND_REVIEW_CONFIG
from config.path import resolve_plan_path
from runtime import StateKey, state_register_db


_PLAN_REF_STATE_KEY = StateKey.PLAN_REF


# Subagent result text is truncated before it enters the extraction prompt so a
# verbose child transcript cannot blow up the review context.
_MAX_SUBAGENT_RESULT_CHARS = 24 * 1024


def _resolve_plan_ref(session_id: str, todos: list[dict]) -> str:
    """Resolve the plan ref: session-level state first, then first todo carrying one."""
    try:
        stored = state_register_db.get_state(session_id, _PLAN_REF_STATE_KEY, "")
    except Exception:
        stored = ""
    if isinstance(stored, str) and stored.strip():
        return stored.strip()
    for todo in todos:
        ref = todo.get("plan_ref")
        if isinstance(ref, str) and ref.strip():
            return ref.strip()
    return ""


_PLAN_CONTEXT_LIST_CAP = 10


#: Run relevance values used by the snapshot filter.
_RELEVANCE_ACTIVE = "active"


_RELEVANCE_UNKNOWN = "unknown"


_RELEVANCE_CANCELLED = "cancelled"


_RELEVANCE_SUPERSEDED = "superseded"


def _read_subagent_runs(session_id: str) -> list[dict]:
    """Collect every descendant run for this session, handoff included (fail-open).

    Descendants (not just direct children) so a grandchild's experience reaches
    the tree: ``_assemble_handoff_tree`` re-parents by ``requester_session_key``.
    ``handoff`` is the dict captured at completion; ``None`` means the child
    did not emit the block and the plan extractor falls back to ``result_text``.
    """
    runs_out: list[dict] = []
    try:
        from agent.tools.subagent.registry import list_descendant_runs

        runs = list_descendant_runs(f"agent:main:session:{session_id}")
    except Exception:
        logger.exception("skill review: failed to list subagent runs for {}", session_id)
        return runs_out

    for run in runs:
        outcome = run.execution.outcome
        result_text = run.completion.result_text or ""
        role = getattr(run.functional_role, "value", str(run.functional_role))
        runs_out.append(
            {
                "task_name": run.task_name,
                "task": run.task,
                "result_text": result_text[:_MAX_SUBAGENT_RESULT_CHARS],
                "outcome": outcome.status.value if outcome else "unknown",
                "error": outcome.error if outcome else None,
                "handoff": run.completion.handoff,
                "step_id": run.step_id,
                "flow_id": run.flow_id,
                "spawn_depth": run.depth,
                "label": run.label,
                "functional_role": role,
                "requester_session_key": run.requester_session_key,
                "child_session_key": run.child_session_key,
            }
        )
    return runs_out


def _read_flow_status(session_id: str, flow_id: str) -> dict:
    """One flow's status + its steps' statuses, or {} (fail-open)."""
    try:
        from agent.tools.taskflow.registry.store_sqlite import get_flow_sync

        flow = get_flow_sync(flow_id, session_id)
    except Exception:
        logger.debug("skill review: failed to read taskflow {}", flow_id, exc_info=True)
        return {}
    if not flow:
        return {}
    steps: dict[str, str] = {}
    for step in (flow.get("state") or {}).get("steps") or []:
        step_id = str(step.get("step_id") or "")
        if step_id:
            steps[step_id] = str(step.get("status") or "")
    return {
        "status": str(flow.get("status") or ""),
        "superseded_by": flow.get("superseded_by"),
        "steps": steps,
    }


def _read_plan_snapshot(session_id: str) -> dict | None:
    """The CURRENT todos + flows, used to filter/re-map runs (None = fail-open).

    Pure read, no writes: the same runs + the same snapshot produce the same
    aggregate, so a retried extraction overwrites idempotently.
    """
    try:
        from agent.tools.todolist.registry.store_sqlite import get_todos_sync

        todos = get_todos_sync(session_id)
    except Exception:
        logger.exception("skill review: failed to read the plan snapshot for {}", session_id)
        return None

    todo_by_step: dict[str, dict] = {}
    flows: dict[str, dict] = {}
    for todo in todos:
        step_id = str(todo.get("step_id") or "").strip()
        if step_id:
            todo_by_step[step_id] = todo
        flow_id = str(todo.get("flow_id") or "").strip()
        if flow_id and flow_id not in flows:
            flows[flow_id] = _read_flow_status(session_id, flow_id)
    return {"todos": todos, "todo_by_step": todo_by_step, "flows": flows}


def _classify_run_relevance(run: dict, snapshot: dict | None) -> str:
    """Why a run is (or is not) part of the CURRENT plan, for the filter.

    ``cancelled`` / ``superseded`` runs are dropped: the branch they belong to
    is gone, so including them would over-extract. A run whose coordinates are
    absent from the snapshot (a free-form spawn, or a todo deleted by a
    replan) stays ``unknown`` — its experience is still world fact.
    """
    if not snapshot:
        return _RELEVANCE_UNKNOWN
    flow = snapshot["flows"].get(str(run.get("flow_id") or ""))
    if flow:
        status = str(flow.get("status") or "").lower()
        if flow.get("superseded_by") or status in (_RELEVANCE_SUPERSEDED, _RELEVANCE_CANCELLED):
            return _RELEVANCE_SUPERSEDED
    step_id = str(run.get("step_id") or "")
    if step_id:
        todo = snapshot["todo_by_step"].get(step_id)
        if todo is None:
            return _RELEVANCE_UNKNOWN
        if str(todo.get("status") or "").lower() == _RELEVANCE_CANCELLED:
            return _RELEVANCE_CANCELLED
        return _RELEVANCE_ACTIVE
    return _RELEVANCE_UNKNOWN


def _resolve_position(run: dict, snapshot: dict | None) -> int | None:
    """The run's step_id → the todo's CURRENT position (None when unresolvable).

    Re-resolved against the live snapshot, so a replan that shifted the list
    cannot silently write the spawn-time position. The extraction prompt is
    told to fall back to task-text matching when this is None.
    """
    if not snapshot:
        return None
    step_id = str(run.get("step_id") or "")
    if not step_id:
        return None
    todo = snapshot["todo_by_step"].get(step_id)
    if todo is None:
        return None
    try:
        return int(todo.get("position"))
    except (TypeError, ValueError):
        return None


def _filter_runs_by_snapshot(runs: list[dict], snapshot: dict | None = None) -> list[dict]:
    """Drop cancelled/superseded runs and attach relevance + resolved_position.

    Runs with no snapshot (None) pass through unchanged (fail-open). The plan
    context's ``subagent_runs`` IS this filtered list, so the extraction LLM
    never re-adds a discarded branch.
    """
    if snapshot is None:
        return list(runs)
    kept: list[dict] = []
    for run in runs:
        relevance = _classify_run_relevance(run, snapshot)
        if relevance in (_RELEVANCE_CANCELLED, _RELEVANCE_SUPERSEDED):
            continue
        enriched = dict(run)
        enriched["relevance"] = relevance
        enriched["resolved_position"] = _resolve_position(run, snapshot)
        kept.append(enriched)
    return kept


def _handoff_label(run: dict) -> str:
    """A readable node label; falls back to the role plus a run-id prefix."""
    label = str(run.get("label") or "").strip()
    if label:
        return label
    role = str(run.get("functional_role") or "subagent")
    run_id = str(run.get("child_session_key") or "")
    return f"{role}-{run_id[-8:]}" if run_id else role


def _assemble_handoff_tree(runs: list[dict]) -> list[dict]:
    """Code-assemble the per-layer handoffs into a parent→child tree.

    Each node's four experience fields stay exactly what ITS layer produced;
    depth/label/outcome/children are injected here (never by the LLM). Orphans
    (a parent was filtered out by the snapshot) are promoted to roots so their
    experience survives.
    """
    children_map: dict[str, list[dict]] = {}
    for run in runs:
        children_map.setdefault(str(run.get("requester_session_key") or ""), []).append(run)
    run_by_child_session = {
        str(run.get("child_session_key")): run for run in runs if run.get("child_session_key")
    }

    def _build_node(run: dict) -> dict | None:
        handoff = run.get("handoff")
        if not isinstance(handoff, dict) or not handoff:
            return None
        node = dict(handoff)
        node["depth"] = run.get("spawn_depth")
        node["agent_label"] = _handoff_label(run)
        node["outcome"] = run.get("outcome")
        node["step_id"] = run.get("step_id")
        node["flow_id"] = run.get("flow_id")
        node["run_id"] = run.get("child_session_key")
        node["children"] = [
            child_node
            for child_node in (
                _build_node(child)
                for child in children_map.get(str(run.get("child_session_key") or ""), [])
            )
            if child_node
        ]
        return node

    def _is_root(run: dict) -> bool:
        requester = str(run.get("requester_session_key") or "")
        if requester.startswith("agent:main:session:"):
            return True
        # Orphan (parent filtered away) → promote so its experience survives.
        return requester not in run_by_child_session

    roots = [_build_node(run) for run in runs if _is_root(run)]
    return [node for node in roots if node]


def _walk_handoff(node: dict, aggregate: dict, seen: set[str], source_prefix: str) -> None:
    """Depth-first walk of one handoff node, flattening into ``aggregate``."""
    depth = node.get("depth", "?")
    label = node.get("agent_label", "?")
    source = f"{source_prefix}depth-{depth}:{label}" if source_prefix else f"depth-{depth}:{label}"
    step_id = node.get("step_id")
    flow_id = node.get("flow_id")
    run_id = node.get("run_id")
    branches = node.get("failed_branches")
    if not isinstance(branches, list):
        # Pre-rename handoffs stored ``failed_paths`` with a ``path`` key.
        branches = node.get("failed_paths") or []
    for failed in branches:
        if not isinstance(failed, dict):
            continue
        aggregate["failed_branches"].append(
            {
                "branch": str(failed.get("branch") or failed.get("path") or ""),
                "failure_reason": str(failed.get("failure_reason") or ""),
                "source": source,
                "step_id": step_id,
                "flow_id": flow_id,
                "run_id": run_id,
            }
        )
    for step in node.get("success_steps", []) or []:
        if not isinstance(step, dict):
            continue
        aggregate["success_steps"].append(
            {
                "step": str(step.get("step") or ""),
                "tool": str(step.get("tool") or ""),
                "params": str(step.get("params") or ""),
                "outcome": str(step.get("outcome") or ""),
                "source": source,
                "step_id": step_id,
                "flow_id": flow_id,
                "run_id": run_id,
            }
        )
    for constraint in node.get("discovered_hard_constraints", []) or []:
        text = str(constraint)
        if text and text not in seen:
            seen.add(text)
            aggregate["hard_constraints"].append(text)
    for decision in node.get("locked_decisions", []) or []:
        if not isinstance(decision, dict):
            continue
        aggregate["locked_decisions"].append(
            {
                "decision": str(decision.get("decision") or ""),
                "reason": str(decision.get("reason") or ""),
                "source": source,
            }
        )
    for child in node.get("children", []) or []:
        _walk_handoff(child, aggregate, seen, source_prefix=f"{source} > ")


def _aggregate_handoffs(tree_roots: list[dict]) -> dict[str, Any]:
    """Flatten the assembled forest (pure; same input → same output)."""
    aggregate: dict[str, Any] = {
        "failed_branches": [],
        "success_steps": [],
        "hard_constraints": [],
        "locked_decisions": [],
        "handoff_forest": tree_roots,
    }
    seen: set[str] = set()
    for root in tree_roots:
        _walk_handoff(root, aggregate, seen, source_prefix="")
    return aggregate


def _compact_aggregate(aggregate: dict[str, Any]) -> dict[str, Any]:
    """The plan-context view: capped flat lists + totals, NO forest.

    Keeps the prompt increment fixed (~500-800 tokens) regardless of how many
    subagents ran; the full tree lives in ``handoff-tree.json`` as the audit
    artifact and is no longer exposed as a model-facing knowledge read.
    """
    failed_branches = aggregate.get("failed_branches") or []
    success_steps = aggregate.get("success_steps") or []
    hard_constraints = aggregate.get("hard_constraints") or []
    locked_decisions = aggregate.get("locked_decisions") or []
    return {
        "failed_branches": failed_branches[:_PLAN_CONTEXT_LIST_CAP],
        "success_steps": success_steps[:_PLAN_CONTEXT_LIST_CAP],
        "hard_constraints": hard_constraints[:_PLAN_CONTEXT_LIST_CAP],
        "locked_decisions": locked_decisions[:_PLAN_CONTEXT_LIST_CAP],
        "total_failed_branches": len(failed_branches),
        "total_success_steps": len(success_steps),
        "total_hard_constraints": len(hard_constraints),
        "total_locked_decisions": len(locked_decisions),
    }


def _row_text(row: dict[str, Any]) -> str:
    """Flatten a persisted message row's content to text."""
    content = row.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(
            str(block.get("text", ""))
            for block in content
            if isinstance(block, dict) and block.get("type") == "text"
        )
    return str(content or "")


def _row_skill_name(call: Any) -> str:
    """Extract a skill name from a LangChain/OpenAI-style tool call."""
    if not isinstance(call, dict):
        return ""
    name = call.get("name")
    args = call.get("args")
    if not name:
        function = call.get("function")
        if isinstance(function, dict):
            name = function.get("name")
            args = function.get("arguments")
    if str(name or "") != "skill_view":
        return ""
    if isinstance(args, str):
        try:
            args = json.loads(args)
        except (TypeError, ValueError):
            return ""
    if isinstance(args, dict):
        return str(args.get("name") or "").strip()
    return ""


def _loaded_skill_details(names: list[str]) -> list[dict[str, str]]:
    """Resolve loaded skill names to their index metadata (best-effort)."""
    if not names:
        return []
    try:
        from skills.loader import scan_skills

        index = {str(skill.get("name") or ""): skill for skill in scan_skills()}
    except Exception:
        index = {}
    result: list[dict[str, str]] = []
    for name in names:
        meta = index.get(name) or {}
        result.append(
            {
                "name": name,
                "description": str(meta.get("description") or ""),
                "when_to_use": str(meta.get("when_to_use") or ""),
            }
        )
    return result


def _recent_usage_skill_names(limit: int) -> list[str]:
    """Recently used auto skills, as a best-effort fallback for preload paths.

    TaskFlow/cron skill preloading bumps ``use_count`` without a transcript
    ``skill_view`` call. This is not plan-scoped, so callers add it only after
    the exact transcript-derived names.
    """
    if limit <= 0:
        return []
    try:
        from agent.tools.pub_base.skill_usage import load_usage

        records = load_usage()
    except Exception:
        return []
    rows = []
    for name, record in records.items():
        if not isinstance(record, dict):
            continue
        stamp = str(record.get("last_used_at") or record.get("last_viewed_at") or "")
        if stamp:
            rows.append((stamp, str(name)))
    rows.sort(reverse=True)
    return [name for _stamp, name in rows[:limit]]


def _collect_skill_review_feedback(session_id: str) -> dict[str, Any]:
    """Bounded user corrections + actually-loaded skills for the skill review.

    Skill review has never received the conversation; the skill-review
    prompt needs the user's explicit corrections and the skills the plan really
    touched. This reads only the recent persisted turns, skips internal carriers
    and system reminders, and caps both lists so the skill-review prompt stays small.
    """
    corrections: list[str] = []
    loaded_names: list[str] = []
    try:
        from context_engine.store.core import get_messages_by_lastest_n_turns

        rows = get_messages_by_lastest_n_turns(
            session_id,
            last_n=max(1, int(BACKGROUND_REVIEW_CONFIG["skill_review_feedback_turns"])),
            only_eligible=True,
        )
    except Exception as exc:  # noqa: BLE001 - advisory context, never load-bearing
        logger.debug("skill review: feedback read failed for {}: {}", session_id, exc)
        return {"loaded_skills": [], "user_corrections": []}

    max_items = max(1, int(BACKGROUND_REVIEW_CONFIG["skill_review_feedback_max_items"]))
    max_chars = max(0, int(BACKGROUND_REVIEW_CONFIG["skill_review_feedback_chars"]))
    for row in reversed(rows or []):
        role = str(row.get("role") or "")
        if role == "human":
            if row.get("origin") not in (None, "user"):
                continue
            text = _row_text(row).strip()
            if (
                not text
                or text.startswith("[SYSTEM DIRECTIVE")
                or text.startswith("<system-reminder")
            ):
                continue
            if len(corrections) >= max_items:
                continue
            corrections.append(text[:500])
            continue
        if role != "ai":
            continue
        for call in row.get("tool_calls") or []:
            name = _row_skill_name(call)
            if name and name not in loaded_names:
                loaded_names.append(name)

    # Chronological order is more useful to a reviewer than newest-first.
    max_loaded = max(1, int(BACKGROUND_REVIEW_CONFIG["skill_review_loaded_skills_max"]))
    for name in _recent_usage_skill_names(max_loaded):
        if name not in loaded_names:
            loaded_names.append(name)
    loaded_names = loaded_names[:max_loaded]
    if max_chars:
        budget = 0
        bounded: list[str] = []
        for text in corrections:
            remaining = max_chars - budget
            if remaining <= 0:
                break
            bounded.append(text[:remaining])
            budget += min(len(text), remaining)
        corrections = bounded
    return {
        "loaded_skills": _loaded_skill_details(loaded_names),
        "user_corrections": corrections,
    }


def _build_plan_context(session_id: str) -> dict[str, Any]:
    """Build the structured context handed to one skill-review pass.

    Reads:
    1. Plan file (``plan_ref`` from session state, else first non-empty todo
       ref), resolved through ``config.path.resolve_plan_path``
       (``workspace/sessions/<session_id>/plans/``)
    2. Todos (todos.db)
    3. Subagent runs (registry queries)

    Returns an empty dict when there is no todo list to extract from; every
    downstream read is fail-open so a missing plan file never breaks the
    extraction pass.
    """
    try:
        from agent.tools.todolist.registry.store_sqlite import get_todos_sync

        todos = get_todos_sync(session_id)
    except Exception:
        logger.exception("skill review: failed to read todos for {}", session_id)
        return {}
    if not todos:
        return {}

    plan_ref = _resolve_plan_ref(session_id, todos)
    resolved = resolve_plan_path(plan_ref, session_id) if plan_ref else None
    plan_name = ""
    plan_content = ""
    if resolved is not None:
        try:
            plan_content = resolved.read_text(encoding="utf-8")
            plan_name = resolved.stem
        except OSError:
            logger.exception("skill review: failed to read plan file {}", resolved)
    if not plan_name:
        # Full-id hash: ids sharing an 8-char prefix must not share a fallback dir.
        from agent.tools.todolist.knowledge.identity import fallback_plan_name

        plan_name = fallback_plan_name(session_id)
    plan_path = str(resolved) if resolved is not None else plan_ref

    # The same identity function the spawn-side lookup uses. A plan name can
    # resolve to a canonical path, or to the session-derived fallback; both
    # paths must produce the SAME key or a compiled script is never found.
    try:
        from agent.tools.todolist.knowledge.identity import (
            resolve_plan_identity,
            resolve_session_plan_identity,
        )

        identity = resolve_plan_identity(session_id, plan_name)
        if identity is None:
            identity = resolve_session_plan_identity(session_id)
        plan_key = identity.key if identity is not None else ""
    except Exception:
        logger.exception("skill review: failed to resolve plan identity for {}", session_id)
        plan_key = ""

    runs = _read_subagent_runs(session_id)
    snapshot = _read_plan_snapshot(session_id)
    filtered = _filter_runs_by_snapshot(runs, snapshot)
    aggregated = _aggregate_handoffs(_assemble_handoff_tree(filtered))

    return {
        "plan_name": plan_name,
        "plan_key": plan_key,
        "plan_path": plan_path,
        "plan_content": plan_content,
        "todos": todos,
        "subagent_runs": filtered,
        "aggregated_handoff": aggregated,
        "feedback": _collect_skill_review_feedback(session_id),
        "plan_snapshot": snapshot,
    }
