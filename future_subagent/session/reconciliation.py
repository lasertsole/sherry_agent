"""Reconciliation logic to identify orphan reasons for sub-agent runs."""

from ..types.registry import SubagentRunRecord, ExecutionStatus


def resolve_subagent_run_orphan_reason(run: SubagentRunRecord) -> str | None:
    """Determine if a running sub-agent is orphaned and return the reason, or None.

    Returns 'no_started_at' if the run lacks a start timestamp, or
    'running_too_long' if it has been running for over an hour.
    """
    if run.execution.status != ExecutionStatus.RUNNING:
        return None

    import time
    if run.execution.started_at is None:
        return "no_started_at"

    elapsed = time.monotonic() - run.execution.started_at
    if elapsed > 3600:  # 1 hour threshold for detecting stuck runs
        return f"running_too_long ({elapsed:.0f}s)"

    return None
