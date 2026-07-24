"""Sub-agent lifecycle end-reason and outcome enums, plus a mapping helper."""

from enum import Enum


class LifecycleEndedReason(str, Enum):
    """Why a sub-agent lifecycle ended."""
    COMPLETE = "complete"
    ERROR = "error"
    KILLED = "killed"
    TIMEOUT = "timeout"
    ORPHANED = "orphaned"


class LifecycleEndedOutcome(str, Enum):
    """Terminal outcome category mirrored from RunOutcomeStatus for lifecycle events."""
    OK = "ok"
    ERROR = "error"
    TIMEOUT = "timeout"
    KILLED = "killed"
    UNKNOWN = "unknown"


def outcome_to_ended_reason(outcome_status: str) -> LifecycleEndedReason:
    """Map a run-outcome status string to the corresponding lifecycle ended reason."""
    if outcome_status == "ok":
        return LifecycleEndedReason.COMPLETE
    if outcome_status == "timeout":
        return LifecycleEndedReason.TIMEOUT
    if outcome_status == "killed":
        return LifecycleEndedReason.KILLED
    # Fall back to ERROR for any unrecognized status
    return LifecycleEndedReason.ERROR
