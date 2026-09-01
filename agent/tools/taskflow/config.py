"""TaskFlow shared constants: table name, initial revision, and status enum."""

from enum import Enum

TABLE_NAME = "task_flows"

# Every flow starts at this revision; every mutation bumps it by exactly 1.
INITIAL_REVISION = 1


class TaskFlowStatus(str, Enum):
    """Lifecycle status of a task flow (openclaw managedFlows semantics)."""

    RUNNING = "running"
    WAITING = "waiting"
    DONE = "done"
    FAILED = "failed"
    CANCELLED = "cancelled"


# Terminal flows are immutable: every mutating tool rejects them.
TERMINAL_STATUSES = frozenset(
    {
        TaskFlowStatus.DONE.value,
        TaskFlowStatus.FAILED.value,
        TaskFlowStatus.CANCELLED.value,
    }
)
