"""Resolve sub-agent role and control scope based on nesting depth."""

from ..types.capability import SubagentSessionRole, ControlScope
from ..config import get_config


def resolve_subagent_capabilities(
    depth: int,
    max_depth: int | None = None,
) -> tuple[SubagentSessionRole, ControlScope]:
    """Determine role and control scope by depth: MAIN at 0, LEAF at max, ORCHESTRATOR otherwise."""
    if max_depth is None:
        max_depth = get_config().max_spawn_depth

    if depth == 0:
        return SubagentSessionRole.MAIN, ControlScope.CHILDREN
    if depth >= max_depth:
        return SubagentSessionRole.LEAF, ControlScope.NONE
    return SubagentSessionRole.ORCHESTRATOR, ControlScope.CHILDREN


def is_subagent_session(session_key: str) -> bool:
    """Check whether a session key belongs to a sub-agent (contains ':subagent:')."""
    return ":subagent:" in session_key


def can_spawn_children(role: SubagentSessionRole) -> bool:
    """Check whether the given role is allowed to spawn child sub-agents."""
    return role in (SubagentSessionRole.MAIN, SubagentSessionRole.ORCHESTRATOR)


def extract_depth_from_session_key(session_key: str) -> int:
    """Count ':subagent:' occurrences in a session key to determine nesting depth."""
    depth = 0
    current = session_key
    while ":subagent:" in current:
        depth += 1
        idx = current.index(":subagent:")
        current = current[idx + len(":subagent:"):]
    return depth
