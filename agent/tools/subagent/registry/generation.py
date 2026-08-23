"""Generation tracking: version management across steer/restart cycles to prevent stale callbacks from superseded runs."""

from ..types.registry import SubagentRunRecord, ExecutionStatus
from . import memory


def next_subagent_run_generation(child_session_key: str) -> int:
    """Return the next generation number for a child session by finding the current max + 1."""
    max_gen = 0
    for run in memory.values():
        if run.child_session_key == child_session_key:
            if run.generation > max_gen:
                max_gen = run.generation
    return max_gen + 1


def is_superseded_run(run: SubagentRunRecord) -> bool:
    """Return True if a newer non-terminal run exists for the same child session (i.e. this run is stale)."""
    if run.execution.status == ExecutionStatus.TERMINAL:
        return False
    for other in memory.values():
        if other.child_session_key == run.child_session_key and other.run_id != run.run_id:
            if other.generation > run.generation and other.execution.status != ExecutionStatus.TERMINAL:
                return True
    return False


def get_latest_run_by_child_session_key(child_session_key: str) -> SubagentRunRecord | None:
    """Return the run with the highest generation for the given child session key."""
    latest: SubagentRunRecord | None = None
    for run in memory.values():
        if run.child_session_key != child_session_key:
            continue
        if latest is None:
            latest = run
        elif run.generation > latest.generation:
            latest = run
    return latest
