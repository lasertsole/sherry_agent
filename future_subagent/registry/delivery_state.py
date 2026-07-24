"""Delivery state machine accessors: query and transition CompletionDeliveryState statuses."""

import time
from ..types.registry import SubagentRunRecord, CompletionDeliveryState, DeliveryStatus, CompletionState


def ensure_delivery_state(run: SubagentRunRecord) -> CompletionDeliveryState:
    """Return the delivery state of a run (identity accessor)."""
    return run.delivery


def ensure_completion_state(run: SubagentRunRecord) -> CompletionState:
    """Return the completion state of a run (identity accessor)."""
    return run.completion


def is_delivery_not_required(run: SubagentRunRecord) -> bool:
    """Return True if delivery is marked NOT_REQUIRED."""
    return run.delivery.status == DeliveryStatus.NOT_REQUIRED


def is_delivery_pending(run: SubagentRunRecord) -> bool:
    """Return True if delivery is PENDING."""
    return run.delivery.status == DeliveryStatus.PENDING


def is_delivery_in_progress(run: SubagentRunRecord) -> bool:
    """Return True if delivery is IN_PROGRESS."""
    return run.delivery.status == DeliveryStatus.IN_PROGRESS


def is_delivery_delivered(run: SubagentRunRecord) -> bool:
    """Return True if delivery has been DELIVERED."""
    return run.delivery.status == DeliveryStatus.DELIVERED


def is_delivery_failed(run: SubagentRunRecord) -> bool:
    """Return True if delivery has FAILED."""
    return run.delivery.status == DeliveryStatus.FAILED


def is_delivery_suspended(run: SubagentRunRecord) -> bool:
    """Return True if delivery is SUSPENDED."""
    return run.delivery.status == DeliveryStatus.SUSPENDED


def is_delivery_discarded(run: SubagentRunRecord) -> bool:
    """Return True if delivery has been DISCARDED."""
    return run.delivery.status == DeliveryStatus.DISCARDED


def is_delivery_terminal(run: SubagentRunRecord) -> bool:
    """Return True if delivery is in a terminal state (DELIVERED or DISCARDED)."""
    return run.delivery.status in (
        DeliveryStatus.DELIVERED,
        DeliveryStatus.DISCARDED,
    )


def get_delivery_attempt_count(run: SubagentRunRecord) -> int:
    """Return the number of delivery attempts for this run."""
    return run.delivery.attempt_count


def get_delivery_last_error(run: SubagentRunRecord) -> str | None:
    """Return the last error message from a failed delivery attempt."""
    return run.delivery.last_error


def mark_delivery_pending(run: SubagentRunRecord) -> SubagentRunRecord:
    """Transition delivery to PENDING status."""
    return run.model_copy(update={
        "delivery": run.delivery.model_copy(update={"status": DeliveryStatus.PENDING})
    })


def mark_delivery_in_progress(run: SubagentRunRecord) -> SubagentRunRecord:
    """Transition delivery to IN_PROGRESS and record the attempt timestamp."""
    return run.model_copy(update={
        "delivery": run.delivery.model_copy(update={
            "status": DeliveryStatus.IN_PROGRESS,
            "last_attempt_at": time.monotonic(),
        })
    })


def mark_delivery_delivered(run: SubagentRunRecord) -> SubagentRunRecord:
    """Transition delivery to DELIVERED and record delivery timestamps."""
    return run.model_copy(update={
        "delivery": run.delivery.model_copy(update={
            "status": DeliveryStatus.DELIVERED,
            "delivered_at": time.monotonic(),
            "announced_at": time.monotonic(),
        })
    })


def mark_delivery_failed(run: SubagentRunRecord, error: str) -> SubagentRunRecord:
    """Transition delivery to FAILED, increment attempt count, and record the error."""
    return run.model_copy(update={
        "delivery": run.delivery.model_copy(update={
            "status": DeliveryStatus.FAILED,
            "last_error": error,
            "attempt_count": run.delivery.attempt_count + 1,
            "last_attempt_at": time.monotonic(),
        })
    })


def mark_delivery_suspended(run: SubagentRunRecord) -> SubagentRunRecord:
    """Transition delivery to SUSPENDED and record the suspension timestamp."""
    return run.model_copy(update={
        "delivery": run.delivery.model_copy(update={
            "status": DeliveryStatus.SUSPENDED,
            "suspended_at": time.monotonic(),
        })
    })


def mark_delivery_discarded(run: SubagentRunRecord, reason: str = "max_retries") -> SubagentRunRecord:
    """Transition delivery to DISCARDED with an optional reason."""
    return run.model_copy(update={
        "delivery": run.delivery.model_copy(update={
            "status": DeliveryStatus.DISCARDED,
            "discard_reason": reason,
        })
    })


def clear_delivery_state(run: SubagentRunRecord) -> SubagentRunRecord:
    """Reset the delivery state to its default (initial) values."""
    return run.model_copy(update={
        "delivery": CompletionDeliveryState()
    })


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
