"""Thinking-level inheritance and override resolution for sub-agents.

Three-layer precedence: explicit override → requester's subagent setting →
target agent's default.
"""

from ..config import get_config


def resolve_thinking_override(
    requester_thinking: str | None = None,
    target_agent_thinking: str | None = None,
    explicit_override: str | None = None,
) -> str | None:
    """Resolve the effective thinking level using the 3-layer precedence hierarchy."""
    if explicit_override and explicit_override.strip():
        return explicit_override.strip()

    if requester_thinking and requester_thinking.strip():
        return requester_thinking.strip()

    if target_agent_thinking and target_agent_thinking.strip():
        return target_agent_thinking.strip()

    return None


def resolve_initial_session_patch(thinking: str | None) -> dict:
    """Build a session-patch dict that injects the thinkingLevel key when a thinking override is active."""
    if not thinking or not thinking.strip():
        return {}
    return {"thinkingLevel": thinking.strip()}
