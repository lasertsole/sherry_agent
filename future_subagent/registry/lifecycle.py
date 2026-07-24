"""Lifecycle management: sub-agent completion, resumption, suspended delivery retry, orphan recovery, and pressure pruning.

Enhancements: async locks for terminal transitions, generation validation to prevent stale callbacks,
kill reconciliation, persistence failure rollback, and deferred descendant scheduling.
"""

import time
import asyncio
from loguru import logger
from ..types.registry import (
    SubagentRunRecord,
    ExecutionStatus,
    DeliveryStatus,
    RunOutcome,
    RunOutcomeStatus,
    ExecutionState,
    KillReconciliationState,
)
from ..types.lifecycle import LifecycleEndedReason, outcome_to_ended_reason
from ..announce.core import run_subagent_announce_flow
from ..registry import (
    complete_run as _complete_run,
    set_run,
    get_run,
    all_runs,
    is_delivery_failed,
    is_delivery_suspended,
    should_retry_delivery,
    mark_delivery_pending,
    mark_delivery_discarded,
    reconcile_orphaned_run,
    is_live_unended_run,
    has_run_ended,
    resolve_finalized_task_state,
)
from ..registry.helpers import safe_remove_attachments_dir, is_stale_unended_run
from ..registry.cleanup import resolve_deferred_cleanup_decision
from ..registry.delivery_state import should_discard_delivery, is_delivery_expired
from ..registry.generation import is_superseded_run
from ..registry.terminal_gen import get_terminal_gen_tracker
from ..registry.settle_wake import get_settle_wake_batch
from ..registry.work_admission import run_with_work_admission
from ..config import get_config

_terminal_locks: dict[str, asyncio.Lock] = {}
_cleanup_generations: dict[str, int] = {}
_deferred_cleanup_timers: dict[str, asyncio.Task] = {}


def _get_terminal_lock(run_id: str) -> asyncio.Lock:
    """Return (or create) an async lock for a run's terminal transition."""
    if run_id not in _terminal_locks:
        _terminal_locks[run_id] = asyncio.Lock()
    return _terminal_locks[run_id]


async def complete_subagent_run(
    run_id: str,
    outcome: RunOutcome,
    result_text: str | None = None,
    expected_generation: int | None = None,
) -> SubagentRunRecord | None:
    """Complete a sub-agent run with the given outcome, guarded by generation and kill reconciliation."""
    lock = _get_terminal_lock(run_id)
    if lock.locked():
        logger.debug("complete_subagent_run: run {} already being completed", run_id)
        return get_run(run_id)

    async with lock:
        run = get_run(run_id)
        if run is None:
            return None

        gen_tracker = get_terminal_gen_tracker()
        if expected_generation is not None:
            if not gen_tracker.is_callback_current(run_id, expected_generation):
                logger.warning(
                    "complete_subagent_run: generation callback stale for run {} (expected={}, actual={}), skipping",
                    run_id, expected_generation, run.generation,
                )
                return run
        elif not gen_tracker.is_callback_current(run_id, run.generation):
            if gen_tracker.is_older_equivalent(run_id, run.generation):
                logger.warning("complete_subagent_run: older equivalent callback for run {}, skipping", run_id)
                return run

        gen_tracker.retire(run_id)

        if is_superseded_run(run):
            logger.warning("complete_subagent_run: run {} is superseded, skipping completion", run_id)
            return run

        run = _arbitrate_kill_vs_completion(run, outcome)
        if run.kill_reconciliation is not None and run.kill_reconciliation.reconciled:
            set_run(run)

        updated = _complete_run(run_id, outcome, result_text)
        if updated is None:
            return None

        updated = _mark_terminal_owner(updated, f"outcome:{outcome.status.value}")
        set_run(updated)

        await _start_announce_cleanup_flow(updated)

        _terminal_locks.pop(run_id, None)
        return updated


def _apply_kill_reconciliation(run: SubagentRunRecord) -> SubagentRunRecord:
    """Finalize kill reconciliation: mark as reconciled regardless of prior state."""
    if run.kill_reconciliation is None or run.kill_reconciliation.reconciled:
        return run

    kr = run.kill_reconciliation
    if kr.snapshot_execution.status == ExecutionStatus.TERMINAL:
        logger.info("Kill reconciliation: run {} was already terminal before kill, keeping kill outcome", run.run_id)
        return run.model_copy(update={
            "kill_reconciliation": kr.model_copy(update={"reconciled": True}),
        })

    logger.info("Kill reconciliation: run {} had kill while running, kill outcome takes precedence", run.run_id)
    return run.model_copy(update={
        "kill_reconciliation": kr.model_copy(update={"reconciled": True}),
    })


def _mark_terminal_owner(run: SubagentRunRecord, owner: str) -> SubagentRunRecord:
    """Record the first owner that triggered terminal transition, if not already set."""
    if run.terminal_owner is not None:
        return run
    return run.model_copy(update={"terminal_owner": owner})


def _arbitrate_kill_vs_completion(run: SubagentRunRecord, outcome: RunOutcome) -> SubagentRunRecord:
    """Resolve conflict between a kill and a concurrent completion callback.

    If the kill snapshot is KILLED and the provider reports OK with a result, the completion
    takes precedence. Otherwise, the kill outcome wins.
    """
    if run.kill_reconciliation is None or run.kill_reconciliation.reconciled:
        return _apply_kill_reconciliation(run)

    kr = run.kill_reconciliation
    snapshot_outcome = kr.snapshot_execution.outcome

    if snapshot_outcome and snapshot_outcome.status == RunOutcomeStatus.KILLED:
        if outcome.status == RunOutcomeStatus.OK and run.completion.result_text:
            logger.info("Kill↔completion arbitration for run {}: provider completion overrides kill", run.run_id)
            return run.model_copy(update={
                "kill_reconciliation": kr.model_copy(update={"reconciled": True}),
                "suppress_completion_delivery": False,
            })
        else:
            logger.info("Kill↔completion arbitration for run {}: kill takes precedence", run.run_id)
            return run.model_copy(update={
                "kill_reconciliation": kr.model_copy(update={"reconciled": True}),
            })

    return _apply_kill_reconciliation(run)


async def _start_announce_cleanup_flow(run: SubagentRunRecord) -> None:
    """Orchestrate post-completion: announce delivery, settle-wake, swarm notification, and cleanup."""
    config = get_config()

    if run.suppress_announce_reason:
        logger.debug("Announce suppressed for run {}: reason={}", run.run_id, run.suppress_announce_reason)
    elif run.suppress_completion_delivery:
        logger.debug("Completion delivery suppressed for run {} (kill arbitration)", run.run_id)
    elif _should_suspend_pending_final_delivery(run):
        _suspend_pending_final_delivery(run)
        return
    elif run.expects_completion_message and run.completion.required:
        try:
            await run_subagent_announce_flow(run)
        except Exception as e:
            logger.error("Announce flow failed for run {}: {}", run.run_id, e)

    settle_batch = get_settle_wake_batch()
    settle_batch.register_run_for_settle(run.run_id, run.requester_session_key)

    if run.swarm_group_id:
        from ..swarm.collector import complete_swarm_run
        try:
            await complete_swarm_run(run.run_id, run.execution.outcome or RunOutcome(status=RunOutcomeStatus.OK), run.completion.result_text)
        except Exception as e:
            logger.debug("complete_swarm_run failed for run {}: {}", run.run_id, e)

    from .queries import count_active_descendant_runs
    if count_active_descendant_runs(run.requester_session_key) == 0:
        settled = await settle_batch.complete_batch(run.requester_session_key)
        if settled:
            settle_batch.retire_after_settle(run.requester_session_key)

    should_cleanup, reason = resolve_deferred_cleanup_decision(run)
    if should_cleanup:
        _cleanup_generations[run.run_id] = run.generation
        await _finalize_cleanup(run, reason)
    elif reason == "defer_descendants":
        logger.info("Cleanup deferred for run {}: active descendants exist", run.run_id)
        _schedule_deferred_cleanup_resume(run, delay_seconds=5.0)
    elif reason.startswith("delivery_"):
        logger.info("Cleanup deferred for run {}: {}", run.run_id, reason)


async def _finalize_cleanup(run: SubagentRunRecord, reason: str) -> None:
    """Execute cleanup: remove attachments and/or child session, then mark cleanup as completed."""
    current_gen = _cleanup_generations.get(run.run_id, run.generation)
    latest = get_run(run.run_id)
    if latest and latest.generation > current_gen:
        logger.info("Cleanup skipped for run {}: generation advanced ({}) > cleanup gen ({})",
                    run.run_id, latest.generation, current_gen)
        _cleanup_generations.pop(run.run_id, None)
        return

    if run.cleanup == "delete" and not _should_retain_attachments(run):
        if run.attachments_dir:
            safe_remove_attachments_dir(run.attachments_dir, run.attachments_root_dir)

        from ..session.cleanup import delete_subagent_session_for_cleanup
        await delete_subagent_session_for_cleanup(run.child_session_key, run.spawn_mode)

    from ..registry import update as update_run
    update_run(run.run_id, cleanup_completed_at=time.monotonic())

    _cleanup_generations.pop(run.run_id, None)
    logger.info("Finalized cleanup for run {}: reason={}, cleanup={}", run.run_id, reason, run.cleanup)

    if run.wake_on_descendant_settle:
        try:
            from ..registry import wake_yield_if_all_children_settled
            await wake_yield_if_all_children_settled(run.requester_session_key)
        except Exception as e:
            logger.debug("Wake check after cleanup failed for run {}: {}", run.run_id, e)


def _should_suspend_pending_final_delivery(run: SubagentRunRecord) -> bool:
    """Return True if a keep-cleanup run with OK outcome should suspend its pending delivery."""
    if run.cleanup != "keep":
        return False
    if run.ended_reason != "complete":
        return False
    if not run.expects_completion_message:
        return False
    if run.execution.outcome is None or run.execution.outcome.status != RunOutcomeStatus.OK:
        return False
    if run.delivery.status != DeliveryStatus.PENDING:
        return False
    return True


def _suspend_pending_final_delivery(run: SubagentRunRecord) -> None:
    """Suspend a pending delivery and schedule a settle-wake retry for the requester."""
    from ..registry import mark_delivery_suspended
    updated = mark_delivery_suspended(run)
    set_run(updated)
    logger.info("Suspended pending final delivery for run {}", run.run_id)

    settle_batch = get_settle_wake_batch()
    settle_batch.schedule_settle_wake_retry(run.requester_session_key, delay=5.0)


def _schedule_deferred_cleanup_resume(run: SubagentRunRecord, delay_seconds: float) -> None:
    """Schedule a delayed re-evaluation of cleanup after descendants have settled."""
    existing = _deferred_cleanup_timers.get(run.run_id)
    if existing and not existing.done():
        return

    async def _resume():
        await asyncio.sleep(delay_seconds)
        _deferred_cleanup_timers.pop(run.run_id, None)
        latest = get_run(run.run_id)
        if latest is None or latest.cleanup_completed_at is not None:
            return
        should, reason = resolve_deferred_cleanup_decision(latest)
        if should:
            _cleanup_generations[latest.run_id] = latest.generation
            await _finalize_cleanup(latest, reason)
        elif reason == "defer_descendants":
            _schedule_deferred_cleanup_resume(latest, delay_seconds=10.0)

    _deferred_cleanup_timers[run.run_id] = asyncio.create_task(_resume())


def _should_retain_attachments(run: SubagentRunRecord) -> bool:
    """Return True if attachments should be kept (keep-cleanup, session mode, or explicit retain flag)."""
    if run.retain_attachments_on_keep:
        return True
    if run.cleanup == "keep":
        return True
    if run.spawn_mode.value == "session":
        return True
    return False


async def resume_subagent_run(run_id: str) -> SubagentRunRecord | None:
    """Resume an INTERRUPTED run back to RUNNING state.

    WARNING: This only updates the registry record. It does NOT re-invoke
    the child agent. The caller is responsible for re-dispatching execution
    (e.g. via steer_subagent_run or by creating a new asyncio.Task).
    Without re-dispatch, the run will appear RUNNING but nothing will execute.
    """
    run = get_run(run_id)
    if run is None:
        return None

    if run.execution.status not in (ExecutionStatus.INTERRUPTED,):
        return run

    updated = run.model_copy(update={
        "execution": run.execution.model_copy(update={
            "status": ExecutionStatus.RUNNING,
            "started_at": time.monotonic(),
        }),
        "generation": run.generation + 1,
        "pause_reason": None,
    })
    set_run(updated)

    try:
        await wake_yield_if_all_children_settled(updated.requester_session_key)
    except Exception:
        pass

    return updated


async def finalize_suspended_deliveries() -> int:
    """Retry or discard suspended deliveries based on retry limits and expiry. Returns the count acted on."""
    config = get_config()
    count = 0
    now = time.monotonic()

    for run in all_runs():
        if not has_run_ended(run):
            continue
        if not is_delivery_suspended(run) and not is_delivery_failed(run):
            continue

        if should_discard_delivery(run, config.max_announce_retry_count, config.announce_hard_expiry_ms, now):
            from ..registry import mark_delivery_discarded as _mark_discarded
            updated = _mark_discarded(run, reason="expiry_or_max_retries")
            set_run(updated)
            count += 1
            continue

        if is_delivery_expired(run, config.announce_expiry_ms, now):
            continue

        if should_retry_delivery(run, config.max_announce_retry_count):
            updated = mark_delivery_pending(run)
            set_run(updated)
            try:
                await run_subagent_announce_flow(updated)
                count += 1
            except Exception as e:
                logger.error("Retry delivery failed for run {}: {}", run.run_id, e)

    return count


async def finalize_failed_deliveries() -> int:
    """Retry failed deliveries that have not exceeded the max retry count. Returns the count retried."""
    config = get_config()
    count = 0

    for run in all_runs():
        if not has_run_ended(run):
            continue
        if not is_delivery_failed(run):
            continue

        if should_retry_delivery(run, config.max_announce_retry_count):
            updated = mark_delivery_pending(run)
            set_run(updated)
            try:
                await run_subagent_announce_flow(updated)
                count += 1
            except Exception as e:
                logger.error("Retry failed delivery for run {}: {}", run.run_id, e)

    return count


async def recover_orphaned_runs() -> int:
    """Find and reconcile live unended runs that have become orphans. Returns the count recovered."""
    count = 0
    for run in all_runs():
        if not is_live_unended_run(run):
            continue
        if is_superseded_run(run):
            continue

        updated = reconcile_orphaned_run(run)
        if updated is not None:
            updated = _mark_terminal_owner(updated, "orphan-recovery")
            set_run(updated)
            await _start_announce_cleanup_flow(updated)
            count += 1

    return count


async def pressure_prune_suspended_deliveries() -> int:
    """Prune oldest suspended deliveries when the soft cap is exceeded. Returns the count pruned."""
    config = get_config()
    now = time.monotonic()
    suspended = [
        run for run in all_runs()
        if has_run_ended(run) and is_delivery_suspended(run)
    ]

    if len(suspended) <= config.delivery_suspend_soft_cap:
        return 0

    pruned = 0
    target_count = max(config.delivery_suspend_target, len(suspended) - config.delivery_suspend_soft_cap)
    to_prune = max(0, len(suspended) - target_count)

    suspended.sort(key=lambda r: r.delivery.suspended_at or 0)
    for run in suspended[:to_prune]:
        from ..registry import mark_delivery_discarded as _mark_discarded
        updated = _mark_discarded(run, reason="pressure_prune")
        set_run(updated)
        pruned += 1

    if pruned > 0:
        logger.warning("Pressure pruned {} suspended deliveries (target={})", pruned, target_count)

    return pruned
