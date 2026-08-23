"""Cleanup decision logic: determines whether a finished sub-agent's session and attachments should be removed.

Implements a 3-way branching strategy: defer-descendants / give-up / cleanup.
"""

import time
from ..types.registry import SubagentRunRecord, DeliveryStatus, ExecutionStatus
from ..types.spawn import SpawnMode
from ..config import get_config


def resolve_cleanup_completion_reason(run: SubagentRunRecord) -> str | None:
    """Return a cleanup reason string if the run's delivery reached a terminal state, else None."""
    if run.spawn_mode == SpawnMode.SESSION:
        return None
    if run.delivery.status == DeliveryStatus.DELIVERED:
        return "delivered"
    if run.delivery.status == DeliveryStatus.DISCARDED:
        return "discarded"
    if run.delivery.status == DeliveryStatus.NOT_REQUIRED:
        return "not_required"
    return None


def resolve_deferred_cleanup_decision(run: SubagentRunRecord) -> tuple[bool, str]:
    """Decide whether cleanup can proceed now or must be deferred.

    Returns (should_cleanup, reason). Cleanup is deferred when descendants are still
    active, or when the delivery has not yet reached a terminal or expired state.
    """
    if run.cleanup == "keep":
        return False, "cleanup=keep"

    if run.spawn_mode == SpawnMode.SESSION:
        return False, "session_mode"

    reason = resolve_cleanup_completion_reason(run)
    if reason is not None:
        return True, reason

    from .queries import count_active_descendant_runs
    active_descendants = count_active_descendant_runs(run.child_session_key)
    if active_descendants > 0:
        return False, "defer_descendants"

    config = get_config()
    now = time.monotonic()

    if run.delivery.attempt_count >= config.max_announce_retry_count:
        if run.delivery.status in (DeliveryStatus.FAILED, DeliveryStatus.SUSPENDED):
            return True, "give_up_max_retries"

    if run.execution.ended_at is not None:
        elapsed_ms = (now - run.execution.ended_at) * 1000
        if elapsed_ms > config.announce_hard_expiry_ms:
            return True, "give_up_hard_expiry"

    return False, f"delivery_status={run.delivery.status}"


def should_cleanup_run(run: SubagentRunRecord) -> bool:
    """Return True if the run is terminal and cleanup is not deferred or already done."""
    if run.cleanup_completed_at is not None:
        return False
    if run.execution.status != ExecutionStatus.TERMINAL:
        return False
    should, _ = resolve_deferred_cleanup_decision(run)
    return should
