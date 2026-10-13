"""Sub-agent nesting depth and concurrent-child limit validation."""

import warnings

from ..config import get_config
from ..capabilities import is_subagent_session, extract_depth_from_session_key


def get_subagent_depth(session_key: str) -> int:
    """Return the nesting depth for a session; 0 for non-sub-agent sessions.

    Tries the run record's depth field first, then falls back to extracting
    depth from the session key string.
    """
    from ..registry import get_run_by_child_session_key

    run = get_run_by_child_session_key(session_key)
    if run is not None and run.depth > 0:
        return run.depth

    if not is_subagent_session(session_key):
        return 0
    return extract_depth_from_session_key(session_key)


def validate_spawn_depth(parent_depth: int) -> tuple[bool, str]:
    """Check whether spawning one more level would exceed the configured max depth."""
    config = get_config()
    child_depth = parent_depth + 1
    if child_depth > config.max_spawn_depth:
        return False, f"Spawn depth {child_depth} exceeds max {config.max_spawn_depth}"
    return True, ""


def validate_concurrent_children(current_count: int) -> tuple[bool, str]:
    """Check whether the parent already has the maximum allowed concurrent children."""
    config = get_config()
    if current_count >= config.max_children_per_agent:
        return (
            False,
            f"Concurrent children {current_count} already at max {config.max_children_per_agent}",
        )
    return True, ""


def validate_global_concurrent(current_count: int) -> tuple[bool, str]:
    """[DEPRECATED] Global concurrency is now queued by the SUBAGENT lane.

    Kept for backward compatibility (same signature and result as before); the
    spawn pipeline no longer calls it, because over-limit runs queue as PENDING
    instead of being refused. Use ``runtime.lane.LaneType.SUBAGENT`` for the
    live global limit.
    """
    warnings.warn(
        "validate_global_concurrent is deprecated; global concurrency is queued by "
        "LaneType.SUBAGENT",
        DeprecationWarning,
        stacklevel=2,
    )
    config = get_config()
    if current_count >= config.max_concurrent:
        return (
            False,
            f"Global concurrent subagents {current_count} already at max {config.max_concurrent}",
        )
    return True, ""
