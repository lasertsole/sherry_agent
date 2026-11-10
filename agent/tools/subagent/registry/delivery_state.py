"""Delivery state machine accessors: query and transition CompletionDeliveryState statuses.

The transition table is the single source of truth (2026-11-09 audit H3):
every ``mark_*`` helper funnels through :func:`transition_delivery`, so an
illegal edge (``DISCARDED`` -> ``DELIVERED``) raises instead of silently
rewriting a terminal record. Same-state transitions are idempotent and keep
working (callers re-mark after crashes/restarts).
"""

import time
from collections.abc import Mapping
from typing import Any

from ..types.registry import (
    SubagentRunRecord,
    CompletionDeliveryState,
    DeliveryStatus,
)


class InvalidDeliveryTransitionError(RuntimeError):
    """A caller asked for an edge the delivery state machine does not allow."""


#: Allowed successor states. Terminal rows have no outgoing non-idempotent edge.
#: ``NOT_REQUIRED`` -> ``DELIVERED`` is the bookkeeping path for a run whose
#: completion was never required (silent replies included).
_ALLOWED_TRANSITIONS: Mapping[DeliveryStatus, frozenset[DeliveryStatus]] = {
    # An initial "not started" state, not a terminal one: a record rebuilt from
    # an older shape (or a fixture) may enter any live state from here.
    DeliveryStatus.NOT_REQUIRED: frozenset(
        {
            DeliveryStatus.PENDING,
            DeliveryStatus.IN_PROGRESS,
            DeliveryStatus.DELIVERED,
            DeliveryStatus.FAILED,
            DeliveryStatus.SUSPENDED,
            DeliveryStatus.DISCARDED,
        }
    ),
    DeliveryStatus.PENDING: frozenset(
        {
            DeliveryStatus.IN_PROGRESS,
            DeliveryStatus.DELIVERED,
            DeliveryStatus.FAILED,
            DeliveryStatus.SUSPENDED,
            DeliveryStatus.DISCARDED,
        }
    ),
    DeliveryStatus.IN_PROGRESS: frozenset(
        {
            DeliveryStatus.DELIVERED,
            DeliveryStatus.FAILED,
            DeliveryStatus.SUSPENDED,
            DeliveryStatus.DISCARDED,
        }
    ),
    DeliveryStatus.FAILED: frozenset(
        {
            DeliveryStatus.PENDING,
            DeliveryStatus.IN_PROGRESS,
            DeliveryStatus.SUSPENDED,
            DeliveryStatus.DISCARDED,
        }
    ),
    DeliveryStatus.SUSPENDED: frozenset(
        {
            DeliveryStatus.PENDING,
            DeliveryStatus.IN_PROGRESS,
            DeliveryStatus.DELIVERED,
            DeliveryStatus.DISCARDED,
        }
    ),
    DeliveryStatus.DELIVERED: frozenset(),
    DeliveryStatus.DISCARDED: frozenset(),
}


def transition_delivery(
    run: SubagentRunRecord,
    target: DeliveryStatus,
    **delivery_updates: Any,
) -> SubagentRunRecord:
    """Move ``run``'s delivery to ``target``, applying ``delivery_updates``.

    Same-state transitions are allowed (idempotent re-marking). Any other edge
    not declared in :data:`_ALLOWED_TRANSITIONS` raises
    :class:`InvalidDeliveryTransitionError` before the record is rebuilt.
    """
    current = run.delivery.status
    if target != current and target not in _ALLOWED_TRANSITIONS.get(current, frozenset()):
        raise InvalidDeliveryTransitionError(
            f"delivery {current.value} -> {target.value} is not allowed for run {run.run_id}"
        )
    return run.model_copy(
        update={"delivery": run.delivery.model_copy(update={"status": target, **delivery_updates})}
    )


def ensure_delivery_state(run: SubagentRunRecord) -> CompletionDeliveryState:
    """Return the delivery state of a run (identity accessor)."""
    return run.delivery


def is_delivery_pending(run: SubagentRunRecord) -> bool:
    """Return True if delivery is PENDING."""
    return run.delivery.status == DeliveryStatus.PENDING


def is_delivery_delivered(run: SubagentRunRecord) -> bool:
    """Return True if delivery has been DELIVERED."""
    return run.delivery.status == DeliveryStatus.DELIVERED


def is_delivery_failed(run: SubagentRunRecord) -> bool:
    """Return True if delivery has FAILED."""
    return run.delivery.status == DeliveryStatus.FAILED


def is_delivery_suspended(run: SubagentRunRecord) -> bool:
    """Return True if delivery is SUSPENDED."""
    return run.delivery.status == DeliveryStatus.SUSPENDED


def is_delivery_terminal(run: SubagentRunRecord) -> bool:
    """Return True if delivery is in a terminal state (DELIVERED or DISCARDED)."""
    return run.delivery.status in (
        DeliveryStatus.DELIVERED,
        DeliveryStatus.DISCARDED,
    )


def get_delivery_attempt_count(run: SubagentRunRecord) -> int:
    """Return the number of delivery attempts for this run."""
    return run.delivery.attempt_count


def mark_delivery_pending(run: SubagentRunRecord) -> SubagentRunRecord:
    """Transition delivery to PENDING status."""
    return transition_delivery(run, DeliveryStatus.PENDING)


def mark_delivery_in_progress(run: SubagentRunRecord) -> SubagentRunRecord:
    """Transition delivery to IN_PROGRESS and record the attempt timestamp."""
    return transition_delivery(run, DeliveryStatus.IN_PROGRESS, last_attempt_at=time.monotonic())


def mark_delivery_delivered(run: SubagentRunRecord) -> SubagentRunRecord:
    """Transition delivery to DELIVERED and record delivery timestamps."""
    now = time.monotonic()
    return transition_delivery(run, DeliveryStatus.DELIVERED, delivered_at=now, announced_at=now)


def mark_delivery_failed(run: SubagentRunRecord, error: str) -> SubagentRunRecord:
    """Transition delivery to FAILED, increment attempt count, and record the error."""
    return transition_delivery(
        run,
        DeliveryStatus.FAILED,
        last_error=error,
        attempt_count=run.delivery.attempt_count + 1,
        last_attempt_at=time.monotonic(),
    )


def mark_delivery_suspended(run: SubagentRunRecord) -> SubagentRunRecord:
    """Transition delivery to SUSPENDED and record the suspension timestamp."""
    return transition_delivery(run, DeliveryStatus.SUSPENDED, suspended_at=time.monotonic())


def mark_delivery_discarded(
    run: SubagentRunRecord, reason: str = "max_retries"
) -> SubagentRunRecord:
    """Transition delivery to DISCARDED with an optional reason."""
    return transition_delivery(run, DeliveryStatus.DISCARDED, discard_reason=reason)


def should_retry_delivery(run: SubagentRunRecord, max_attempts: int = 10) -> bool:
    """Return True if delivery is FAILED or SUSPENDED and attempt count is below max."""
    if run.delivery.status not in (DeliveryStatus.FAILED, DeliveryStatus.SUSPENDED):
        return False
    return run.delivery.attempt_count < max_attempts


def is_delivery_expired(run: SubagentRunRecord, expiry_ms: int, now: float | None = None) -> bool:
    """Return True if the time since last delivery attempt exceeds expiry_ms."""
    if run.delivery.last_attempt_at is None:
        return False
    now = now or time.monotonic()
    elapsed_ms = (now - run.delivery.last_attempt_at) * 1000
    return elapsed_ms > expiry_ms


def should_discard_delivery(
    run: SubagentRunRecord,
    max_announce_retry_count: int,
    hard_expiry_ms: int,
    now: float | None = None,
) -> bool:
    """Return True if delivery should be discarded due to max retries or hard expiry."""
    now = now or time.monotonic()

    if run.delivery.attempt_count >= max_announce_retry_count:
        return True

    if run.execution.ended_at is not None:
        elapsed_ms = (now - run.execution.ended_at) * 1000
        if elapsed_ms > hard_expiry_ms:
            return True

    return False
