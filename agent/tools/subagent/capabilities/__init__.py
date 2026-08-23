"""Role and capability resolution for sub-agents."""

from .core import resolve_subagent_capabilities, is_subagent_session, can_spawn_children, extract_depth_from_session_key

__all__ = [
    "resolve_subagent_capabilities",
    "is_subagent_session",
    "can_spawn_children",
    "extract_depth_from_session_key",
]
