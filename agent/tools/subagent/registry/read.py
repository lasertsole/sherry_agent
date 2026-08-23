"""Read-only query wrappers: provide non-mutating query entry points for external modules.

Merges in-memory snapshot with persisted state to offer a consistent view for UI,
announce, control, and recovery paths.
"""

from . import memory
from . import queries
from ..types.registry import SubagentRunRecord


def get_run_readonly(run_id: str) -> SubagentRunRecord | None:
    """Return a run record by ID without modifying state."""
    return memory.get(run_id)


def list_runs_for_requester_readonly(requester_session_key: str) -> list[SubagentRunRecord]:
    """Return all runs for a requester session without modifying state."""
    return queries.list_runs_for_requester(requester_session_key)


def list_runs_for_controller_readonly(controller_session_key: str) -> list[SubagentRunRecord]:
    """Return all runs visible to a controller session without modifying state."""
    return queries.list_runs_for_controller(controller_session_key)


def list_descendant_runs_readonly(session_key: str) -> list[SubagentRunRecord]:
    """Return all descendant runs for a session without modifying state."""
    return queries.list_descendant_runs(session_key)


def count_active_runs_readonly(session_key: str) -> int:
    """Count active runs for a session without modifying state."""
    return queries.count_active_runs_for_session(session_key)


def count_active_descendant_runs_readonly(root_session_key: str) -> int:
    """Count active descendant runs without modifying state."""
    return queries.count_active_descendant_runs(root_session_key)


def get_run_by_child_session_key_readonly(child_session_key: str) -> SubagentRunRecord | None:
    """Look up a run by child session key without modifying state."""
    return memory.find_by_child_session_key(child_session_key)


def get_snapshot_readonly() -> dict[str, SubagentRunRecord]:
    """Return a shallow copy of all run records without modifying state."""
    return memory.snapshot()


def build_read_index_readonly() -> dict[str, list[SubagentRunRecord]]:
    """Build a requester-keyed index without modifying state."""
    return queries.build_read_index()
