"""Run reconciliation functions: infer completion from session state for orphan detection and late-arriving completions.

Provides:
- resolve_run_orphan_reason: determine if a run has become orphaned
- resolve_completion_from_session: infer completion when session is missing but run is still RUNNING
- resolve_session_started_at / resolve_session_status: derive session info from run records
- resolve_completion_from_session_entry: infer completion from a session entry
"""

import time
from pydantic import BaseModel
from ..types.registry import SubagentRunRecord, ExecutionStatus, RunOutcome, RunOutcomeStatus


class SubagentSessionCompletion(BaseModel):
    """Represents an inferred completion result for a sub-agent session."""
    started_at: float | None = None
    ended_at: float
    outcome: RunOutcome
    reason: str


def resolve_run_orphan_reason(
    run: SubagentRunRecord,
    session_exists: bool | None = None,
    stale_unended: bool = False,
) -> str | None:
    """Determine if a run has become orphaned.

    Priority order:
    1. Not RUNNING → not orphaned
    2. No started_at → anomaly
    3. Session missing while running → session_missing_while_running
    4. Running over 3600s → running_too_long
    """
    if run.execution.status != ExecutionStatus.RUNNING:
        return None

    if session_exists is False:
        return "session_missing_while_running"

    if run.execution.started_at is None:
        return "no_started_at"

    elapsed = time.monotonic() - run.execution.started_at
    if elapsed > 3600:
        return f"running_too_long ({elapsed:.0f}s)"

    return None


def resolve_completion_from_session(
    run: SubagentRunRecord,
    session_exists: bool,
) -> SubagentSessionCompletion | None:
    """If the session no longer exists but the run is still RUNNING, infer an error completion."""
    if session_exists:
        return None
    if run.execution.status != ExecutionStatus.RUNNING:
        return None

    return SubagentSessionCompletion(
        ended_at=time.monotonic(),
        outcome=RunOutcome(status=RunOutcomeStatus.ERROR, error="session_missing_while_running"),
        reason="error",
    )


def resolve_completion_from_session_entry(
    session_status: str | None,
    session_ended_at: float | None,
    session_updated_at: float | None,
    fallback_ended_at: float,
) -> SubagentSessionCompletion | None:
    """Infer completion from a session entry's status.

    Mapping: done → ok, timeout → timeout, failed → error, killed → killed.
    """
    ended_at = session_ended_at or session_updated_at or fallback_ended_at

    if session_status == "done":
        return SubagentSessionCompletion(
            ended_at=ended_at,
            outcome=RunOutcome(status=RunOutcomeStatus.OK),
            reason="complete",
        )
    if session_status == "timeout":
        return SubagentSessionCompletion(
            ended_at=ended_at,
            outcome=RunOutcome(status=RunOutcomeStatus.TIMEOUT),
            reason="complete",
        )
    if session_status == "failed":
        return SubagentSessionCompletion(
            ended_at=ended_at,
            outcome=RunOutcome(status=RunOutcomeStatus.ERROR, error="session completed before registry settled"),
            reason="error",
        )
    if session_status == "killed":
        return SubagentSessionCompletion(
            ended_at=ended_at,
            outcome=RunOutcome(status=RunOutcomeStatus.KILLED, error="subagent run terminated"),
            reason="killed",
        )

    return None


def resolve_session_started_at(run: SubagentRunRecord) -> float | None:
    """Derive the session start time from the run record."""
    return run.execution.started_at


def resolve_session_status(run: SubagentRunRecord) -> str:
    """Derive the session status from the run record's execution status."""
    return run.execution.status
