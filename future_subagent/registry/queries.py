"""Read-only query functions over the in-memory store, used by spawn/announce/control modules."""

from ..types.registry import SubagentRunRecord, ExecutionStatus, DeliveryStatus
from . import memory


def list_runs_for_requester(requester_session_key: str) -> list[SubagentRunRecord]:
    """Return all runs whose requester matches the given session key."""
    return [
        run for run in memory.values()
        if run.requester_session_key == requester_session_key
    ]


def list_descendant_runs(requester_session_key: str) -> list[SubagentRunRecord]:
    """BFS-collect all descendant runs rooted at the given requester session key."""
    result: list[SubagentRunRecord] = []
    queue = [requester_session_key]
    visited: set[str] = set()
    while queue:
        current = queue.pop(0)
        if current in visited:
            continue
        visited.add(current)
        children = [
            run for run in memory.values()
            if run.requester_session_key == current
        ]
        for child in children:
            result.append(child)
            queue.append(child.child_session_key)
    return result


def count_active_runs_for_session(session_key: str) -> int:
    """Count runs with RUNNING status for the given requester session."""
    return sum(
        1 for run in memory.values()
        if run.requester_session_key == session_key
        and run.execution.status == ExecutionStatus.RUNNING
    )


def count_active_descendant_runs(session_key: str) -> int:
    """Count RUNNING-status runs among all descendants of the given session."""
    descendants = list_descendant_runs(session_key)
    return sum(
        1 for run in descendants
        if run.execution.status == ExecutionStatus.RUNNING
    )


def count_pending_descendant_runs(session_key: str) -> int:
    """Count PENDING or IN_PROGRESS delivery-status runs among all descendants."""
    descendants = list_descendant_runs(session_key)
    return sum(
        1 for run in descendants
        if run.delivery.status in (DeliveryStatus.PENDING, DeliveryStatus.IN_PROGRESS)
    )


def get_run_by_child_session_key(child_session_key: str) -> SubagentRunRecord | None:
    """Look up a run by its child_session_key."""
    return memory.find_by_child_session_key(child_session_key)


def build_read_index() -> dict[str, list[SubagentRunRecord]]:
    """Build a requester_session_key → list[run] lookup index."""
    index: dict[str, list[SubagentRunRecord]] = {}
    for run in memory.values():
        key = run.requester_session_key
        index.setdefault(key, []).append(run)
    return index


def find_run_by_task_name(requester_session_key: str, task_name: str) -> SubagentRunRecord | None:
    """Find a run matching both requester session key and task name."""
    for run in memory.values():
        if run.requester_session_key == requester_session_key and run.task_name == task_name:
            return run
    return None


def list_runs_for_controller(controller_session_key: str) -> list[SubagentRunRecord]:
    """Return runs where the session is either the requester or the child (i.e. controller-visible)."""
    return [
        run for run in memory.values()
        if run.requester_session_key == controller_session_key
        or run.child_session_key == controller_session_key
    ]
