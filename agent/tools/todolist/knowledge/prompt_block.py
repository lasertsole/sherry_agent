"""Tier-1 plan-knowledge block for the system prompt.

``build_knowledge_block`` resolves the session's primary plan identity
(``identity.resolve_session_plan_identity``: state ``plan_ref`` first, a todo's
``plan_ref`` second, a boulder work third, the session-derived fallback last),
reads that identity's ``plan-summary.json`` through the knowledge store key,
and renders a compact, compression-proof summary (method + capped
key_failures / key_successes / reusable_patterns). Full detail is fetched on
demand through ``knowledge(action="read")``. Every path is fail-open: no
identity, a missing or malformed summary, or any read failure yields "".
"""

from .identity import PlanIdentity, resolve_session_plan_identity
from .knowledge_store import KnowledgeStore


def _trunc(text: str, max_len: int) -> str:
    """Flatten whitespace then clip to one line with an ellipsis marker."""
    flat = " ".join(text.split())
    return flat[:max_len] + "..." if len(flat) > max_len else flat


def _resolve_identity(session_id: str) -> PlanIdentity | None:
    """Resolve the session's primary plan identity; None on any failure."""
    try:
        return resolve_session_plan_identity(session_id)
    except Exception:
        return None


def build_knowledge_block(session_id: str) -> str:
    """Render the current plan's knowledge summary; "" on none or any failure."""
    try:
        identity = _resolve_identity(session_id)
        if identity is None:
            return ""
        summary = KnowledgeStore.read_summary(identity)
        if summary is None:
            return ""
        plan_name = identity.plan_name
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
