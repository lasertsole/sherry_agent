"""Per-step status transitions with one guard (2026-11-09 audit M9).

The resume handler used to assign ``step["status"]`` directly in six branches.
The table here is the single place an edge is legal; a duplicated or delayed
resume can no longer reopen a terminal step. Same-state writes stay idempotent.
"""

from __future__ import annotations

from typing import Any

from .config import StepStatus

__all__ = ["InvalidStepTransitionError", "set_step_status"]


class InvalidStepTransitionError(RuntimeError):
    """A caller asked for a step-status edge the DAG state machine forbids."""


_LIVE = frozenset(
    {
        StepStatus.BLOCKED,
        StepStatus.READY,
        StepStatus.DISPATCHED,
    }
)
_TERMINAL = frozenset(
    {
        StepStatus.DONE,
        StepStatus.FAILED,
        StepStatus.SKIPPED,
        StepStatus.CANCELLED,
    }
)

_ALLOWED_TRANSITIONS: dict[str, frozenset[str]] = {
    # A blocked step can become ready/dispatched when its dependencies settle,
    # and a resume may report any terminal outcome for it.
    StepStatus.BLOCKED.value: frozenset({status.value for status in _LIVE | _TERMINAL}),
    StepStatus.READY.value: frozenset({status.value for status in _LIVE | _TERMINAL}),
    StepStatus.DISPATCHED.value: frozenset({status.value for status in _LIVE | _TERMINAL}),
    # Terminal steps have no outgoing non-idempotent edge.
    StepStatus.DONE.value: frozenset(),
    StepStatus.FAILED.value: frozenset(),
    StepStatus.SKIPPED.value: frozenset(),
    StepStatus.CANCELLED.value: frozenset(),
}


def set_step_status(
    step: dict[str, Any],
    status: StepStatus,
    **fields: Any,
) -> None:
    """Move ``step`` to ``status``, applying reason fields, with one guard."""
    current = str(step.get("status") or "")
    target = status.value
    if target != current and target not in _ALLOWED_TRANSITIONS.get(current, frozenset()):
        raise InvalidStepTransitionError(
            f"step {step.get('step_id') or step.get('id')!r}: {current!r} -> {target!r} "
            "is not allowed"
        )
    step["status"] = target
    step.update(fields)
