"""TaskFlow shared constants: table name, initial revision, and status enum."""

from enum import StrEnum

TABLE_NAME = "task_flows"

# Every flow starts at this revision; every mutation bumps it by exactly 1.
INITIAL_REVISION = 1


class TaskFlowStatus(StrEnum):
    """Lifecycle status of a task flow (openclaw managedFlows semantics)."""

    RUNNING = "running"
    WAITING = "waiting"
    DONE = "done"
    FAILED = "failed"
    CANCELLED = "cancelled"


class StepStatus(StrEnum):
    """Per-step DAG status: blocked -> ready -> dispatched -> done.

    ``done`` means "a result was injected", not "the child succeeded";
    failure-aware transitions are deliberately deferred to a later phase.
    """

    BLOCKED = "blocked"
    READY = "ready"
    DISPATCHED = "dispatched"
    DONE = "done"


# Terminal flows are immutable: every mutating tool rejects them.
TERMINAL_STATUSES = frozenset(
    {
        TaskFlowStatus.DONE.value,
        TaskFlowStatus.FAILED.value,
        TaskFlowStatus.CANCELLED.value,
    }
)
