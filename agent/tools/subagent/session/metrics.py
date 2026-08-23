"""Runtime metrics collection for sub-agent sessions."""

from ..types.registry import SubagentRunRecord, ExecutionStatus


def resolve_subagent_session_started_at(run: SubagentRunRecord) -> float | None:
    """Return the monotonic timestamp when the session started."""
    return run.execution.started_at


def resolve_subagent_session_status(run: SubagentRunRecord) -> str:
    """Return the current execution status string of the session."""
    return run.execution.status


def get_subagent_session_runtime_ms(run: SubagentRunRecord) -> float | None:
    """Compute elapsed runtime in milliseconds; uses current time if the run is still active."""
    if run.execution.started_at is None:
        return None
    end = run.execution.ended_at
    if end is None:
        import time
        end = time.monotonic()
    return (end - run.execution.started_at) * 1000
