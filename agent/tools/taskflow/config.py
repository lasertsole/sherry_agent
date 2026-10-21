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
    """Per-step DAG status.

    Happy path: ``blocked -> ready -> dispatched -> done``. A step that reports
    a definitive failure lands ``failed`` (its dependents stay ``blocked`` —
    they are never unlocked by a failure); a failed/skipped dependency cascades
    ``skipped`` into its dependents; ``cancelled`` is set by the flow-level
    cancel tool for steps that never produced a result.

    ``done`` means "the result was injected AND its quality gate passed" — with
    no expectation configured (no ``response_schema`` / ``judge_criteria`` /
    ``retry_policy``) it still means "a result was injected", for compatibility.
    """

    BLOCKED = "blocked"
    READY = "ready"
    DISPATCHED = "dispatched"
    DONE = "done"
    FAILED = "failed"
    SKIPPED = "skipped"
    CANCELLED = "cancelled"


#: Step statuses that end a step's life without unlocking anything.
UNRESOLVED_STEP_STATUSES = frozenset(
    {
        StepStatus.FAILED.value,
        StepStatus.SKIPPED.value,
        StepStatus.CANCELLED.value,
    }
)


class StepOutcome(StrEnum):
    """How a step's execution ended, as reported by its caller (or derived).

    Kept separate from :class:`StepStatus` because the DAG status is the *effect*
    on the graph (``done`` unlocks, ``failed`` blocks) while the outcome is the
    *fact* about the run (a step may be ``done`` with a ``partial`` outcome).
    """

    SUCCESS = "success"
    FAILURE = "failure"
    PARTIAL = "partial"
    SKIPPED = "skipped"


# Terminal flows are immutable: every mutating tool rejects them.
TERMINAL_STATUSES = frozenset(
    {
        TaskFlowStatus.DONE.value,
        TaskFlowStatus.FAILED.value,
        TaskFlowStatus.CANCELLED.value,
    }
)
