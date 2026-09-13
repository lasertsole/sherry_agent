"""Tier-1 plan-knowledge block for the system prompt.

``build_knowledge_block`` resolves the session's plan name (the ``plan_ref``
state key first, a todo's ``plan_ref`` second), reads the plan's
``plan-summary.json`` from ``config.path.PLAN_KNOWLEDGE_DIR`` and renders a
compact, compression-proof summary (method + capped key_failures /
key_successes / reusable_patterns). Full detail is fetched on demand through
``knowledge(action="read")``. Every path is fail-open: no plan_ref, a missing
or malformed summary, or any read failure yields "".
"""

import json
from pathlib import Path

_PLAN_REF_STATE_KEY = "plan_ref"


def _trunc(text: str, max_len: int) -> str:
    """Flatten whitespace then clip to one line with an ellipsis marker."""
    flat = " ".join(text.split())
    return flat[:max_len] + "..." if len(flat) > max_len else flat


def _resolve_plan_name(session_id: str) -> str:
    """Resolve the session's plan name: state key first, todo plan_ref second."""
    plan_ref = ""
    try:
        from runtime import state_register_db

        value = state_register_db.get_state(session_id, _PLAN_REF_STATE_KEY, "")
        if isinstance(value, str):
            plan_ref = value
    except Exception:
        plan_ref = ""
    if not plan_ref:
        try:
            from agent.tools.todolist.registry.store_sqlite import get_todos_sync

            for todo in get_todos_sync(session_id):
                candidate = todo.get("plan_ref")
                if candidate:
                    plan_ref = str(candidate)
                    break
        except Exception:
            plan_ref = ""
    return Path(plan_ref).stem if plan_ref else ""


def build_knowledge_block(session_id: str) -> str:
    """Render the current plan's knowledge summary; "" on none or any failure."""
    try:
        plan_name = _resolve_plan_name(session_id)
        if not plan_name:
            return ""
        from config.path import PLAN_KNOWLEDGE_DIR

        summary_path = PLAN_KNOWLEDGE_DIR / plan_name / "plan-summary.json"
        if not summary_path.is_file():
            return ""
        summary = json.loads(summary_path.read_text(encoding="utf-8"))
        if not isinstance(summary, dict):
            return ""
        lines = [
            f"## Knowledge Summary: {plan_name}",
            f"Method: {_trunc(str(summary.get('method') or 'unknown'), 200)}",
        ]
        failures = summary.get("key_failures") or []
        if failures:
            lines.append("Key Failures (avoid repeating):")
            lines.extend(
                f"  {index}. {_trunc(str(item), 200)}" for index, item in enumerate(failures[:5], 1)
            )
        successes = summary.get("key_successes") or []
        if successes:
            lines.append("Key Successes:")
            lines.extend(
                f"  {index}. {_trunc(str(item), 200)}"
                for index, item in enumerate(successes[:5], 1)
            )
        patterns = summary.get("reusable_patterns") or []
        if patterns:
            lines.append("Reusable Patterns:")
            lines.extend(f"  - {_trunc(str(item), 150)}" for item in patterns[:8])
        lines.append("Use knowledge(action='read') tool for detailed failure_set/success_path.")
        return "\n".join(lines)
    except Exception:
        return ""


__all__ = ["build_knowledge_block"]
