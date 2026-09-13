"""Background sweeper daemon: periodically recovers orphans, retries suspended deliveries, detects stale runs, pressure-prunes, and persists to disk."""

# allow: SIZE_OK — every scanner shares one sweep loop, one backoff, and one
# persist; forking the TaskFlow scans into another module would split the
# lifecycle they are driven by.

import asyncio
import time
from loguru import logger
from runtime.periodic_backoff import PeriodicBackoff
from ..config import get_config
from ..types.registry import ExecutionStatus

_backoff: PeriodicBackoff | None = None


def _get_backoff() -> PeriodicBackoff:
    """Lazily build the sweeper's backoff state from the configured base interval (avoids reading config at import time)."""
    global _backoff
    if _backoff is None:
        _backoff = PeriodicBackoff(base_interval=float(get_config().sweeper_interval_seconds))
    return _backoff


async def _sweep_loop() -> None:
    """Main sweeper loop: runs at the configured interval until stopped."""
    global _running
    _running = True
    config = get_config()
    interval = config.sweeper_interval_seconds

    logger.info("Subagent sweeper started (interval={}s)", interval)

    while _running:
        await asyncio.sleep(_get_backoff().current_interval)
        try:
            await _do_sweep()
        except Exception as e:
            backoff = _get_backoff()
            backoff.record_failure(repr(e))
            logger.warning(
                "Sweeper failed (consecutive={}, next_interval={}s): {}",
                backoff.consecutive_failures,
                backoff.current_interval,
                e,
            )
            if backoff.is_exhausted():
                logger.critical(
                    "Sweeper backoff exhausted after {} consecutive failures; stopping loop: {}",
                    backoff.consecutive_failures,
                    backoff.reason,
                )
                _running = False
        else:
            _get_backoff().record_success()


async def _do_sweep() -> None:
    """Execute one full sweep cycle: orphans, suspended deliveries, failed deliveries, pressure prune, and persist."""
    from .lifecycle import (
        recover_orphaned_runs,
        finalize_suspended_deliveries,
        finalize_failed_deliveries,
        pressure_prune_suspended_deliveries,
        sweep_stale_lifecycle_state,
    )
    from .work_admission import run_with_work_admission
    from ..orphan.recovery import (
        scan_orphaned_sessions,
        schedule_orphan_recovery,
        reclassify_legacy_timeout,
    )

    orphans = await recover_orphaned_runs()
    if orphans > 0:
        logger.info("Sweeper recovered {} orphaned runs", orphans)

    orphaned_sessions = await scan_orphaned_sessions()
    for orun in orphaned_sessions:
        if orun.ended_reason == "wedged_recovery":
            logger.info("Sweeper: skipping wedged run {} (already finalized)", orun.run_id)
            continue
        if orun.aborted_last_run and orun.ended_reason == "timeout":
            reclassified = reclassify_legacy_timeout(orun)
            if reclassified:
                logger.info("Sweeper reclassified legacy timeout for run {}", orun.run_id)
        else:
            await schedule_orphan_recovery(orun.run_id)

    retried = await finalize_suspended_deliveries()
    if retried > 0:
        logger.info("Sweeper retried {} suspended deliveries", retried)

    expired = await _expire_suspended_by_requester_type()
    if expired > 0:
        logger.info("Sweeper expired {} suspended deliveries by requester type", expired)

    failed_retried = await finalize_failed_deliveries()
    if failed_retried > 0:
        logger.info("Sweeper retried {} failed deliveries", failed_retried)

    pruned = await pressure_prune_suspended_deliveries()
    if pruned > 0:
        logger.warning("Sweeper pressure pruned {} suspended deliveries", pruned)

    await _finalize_killed_unterminated()

    swept = sweep_stale_lifecycle_state()
    if swept > 0:
        logger.debug("Sweeper pruned {} stale lifecycle entries", swept)

    expired_flows = await _expire_overdue_taskflows()
    if expired_flows > 0:
        logger.warning("Sweeper: {} TaskFlow(s) exceeded deadline, marked as failed", expired_flows)

    stale_waiting = await _scan_stale_waiting_taskflows()
    if stale_waiting > 0:
        logger.warning("Sweeper: {} TaskFlow(s) in stale WAITING state", stale_waiting)

    await run_with_work_admission(_persist_async(), label="sweeper-persist")


async def _expire_overdue_taskflows() -> int:
    """Mark non-terminal TaskFlows whose deadline has passed as failed."""
    from agent.tools.taskflow.config import TaskFlowStatus
    from agent.tools.taskflow.registry import store_sqlite as taskflow_store

    try:
        overdue = await taskflow_store.get_overdue_flows(time.time())
        expired = 0
        for flow in overdue:
            deadline_ts = flow["deadline_ts"]
            try:
                state = dict(flow.get("state") or {})
                state["failure_reason"] = "Deadline exceeded: " + time.strftime(
                    "%Y-%m-%d %H:%M", time.localtime(deadline_ts)
                )
                await taskflow_store.update_flow(
                    flow["flow_id"],
                    flow["expected_revision"],
                    state=state,
                    status=TaskFlowStatus.FAILED.value,
                )
                expired += 1
                logger.warning("TaskFlow '{}' deadline exceeded, marked failed", flow["flow_id"])
            except Exception as e:
                logger.warning("Failed to expire TaskFlow '{}': {}", flow["flow_id"], e)
        return expired
    except Exception as e:
        logger.warning("TaskFlow deadline sweep failed: {}", e)
        return 0


async def _scan_stale_waiting_taskflows() -> int:
    """Scan TaskFlows in WAITING state whose ``wait.set_at`` exceeds the timeout.

    For each stale flow, check whether the associated child session is still
    live. A dead (or unknown) child gets a ``stale_detected_at`` marker stamped
    into the wait payload so ``taskflow_summary`` can surface it. The flow is
    never auto-failed: the marker is advisory, and a later sweeper cycle simply
    refreshes it. Fails open (returns 0) so a TaskFlow hiccup never takes the
    subagent sweeper down.
    """
    from config.features import TASKFLOW_INFRA
    from agent.tools.taskflow.registry import store_sqlite as taskflow_store

    try:
        await taskflow_store.ensure_db()
        waiting_flows = await taskflow_store.get_waiting_flows()
        if not waiting_flows:
            return 0

        now = time.time()
        timeout_secs = TASKFLOW_INFRA["waiting_timeout_hours"] * 3600
        stale_count = 0

        for flow in waiting_flows:
            wait = flow.get("wait") or {}
            set_at = wait.get("set_at")
            if not set_at:
                continue

            waiting_secs = now - float(set_at)
            if waiting_secs <= timeout_secs:
                continue

            child_key = flow.get("child_session_key")
            child_live = False
            if child_key:
                try:
                    from agent.tools.subagent.registry.queries import get_run_by_child_session_key
                    from agent.tools.subagent.registry.helpers import is_live_unended_run

                    run = get_run_by_child_session_key(child_key)
                    child_live = run is not None and is_live_unended_run(run)
                except Exception:
                    # Liveness cannot be verified -> treat the child as dead.
                    child_live = False

            if child_live:
                logger.debug(
                    "TaskFlow '{}' waiting {:.1f}h but child session is still live",
                    flow["flow_id"],
                    waiting_secs / 3600,
                )
                continue

            stale_count += 1
            logger.warning(
                "TaskFlow '{}' in stale WAITING ({:.1f}h, child={}): "
                "child session appears inactive; consider resume or re-dispatch",
                flow["flow_id"],
                waiting_secs / 3600,
                child_key,
            )

            # Non-destructive marker: refresh the wait payload in place.
            try:
                wait["stale_detected_at"] = now
                wait["stale_child_session_key"] = child_key
                await taskflow_store.update_flow(
                    flow["flow_id"],
                    flow["expected_revision"],
                    wait=wait,
                )
            except Exception as e:
                logger.debug("Could not mark TaskFlow '{}' as stale: {}", flow["flow_id"], e)

        return stale_count
    except Exception as e:
        logger.warning("TaskFlow WAITING sweep failed: {}", e)
        return 0


async def _expire_suspended_by_requester_type() -> int:
    """Expire suspended deliveries based on requester type (cron/subagent/interactive) with different TTLs."""
    from . import memory, mark_delivery_discarded, set_run

    _REQUESTER_TYPE_EXPIRY_MS = {  # noqa: N806
        "cron": 2 * 3600 * 1000,  # 2 hours for cron
        "subagent": 6 * 3600 * 1000,  # 6 hours for sub-agent
        "interactive": 24 * 3600 * 1000,  # 24 hours for interactive
    }

    now = time.monotonic()
    expired = 0

    for run in memory.values():
        if run.delivery.status.value != "suspended":
            continue
        if not run.delivery.suspended_at:
            continue

        elapsed_ms = (now - run.delivery.suspended_at) * 1000

        requester_key = run.requester_session_key or ""
        if ":cron:" in requester_key:
            expiry_ms = _REQUESTER_TYPE_EXPIRY_MS["cron"]
        elif ":subagent:" in requester_key:
            expiry_ms = _REQUESTER_TYPE_EXPIRY_MS["subagent"]
        else:
            expiry_ms = _REQUESTER_TYPE_EXPIRY_MS["interactive"]

        if elapsed_ms > expiry_ms:
            updated = mark_delivery_discarded(run, reason="requester_type_expiry")
            set_run(updated)
            expired += 1

    return expired


async def _persist_async() -> None:
    """Trigger an asynchronous persist of in-memory state to disk."""
    from .state import persist_runs_to_disk

    await persist_runs_to_disk()


async def _finalize_killed_unterminated() -> int:
    """Finalize cleanup for killed runs that are terminal but not yet cleaned up."""
    from . import memory

    count = 0
    for run in memory.values():
        if run.execution.status != ExecutionStatus.TERMINAL:
            continue
        if run.ended_reason != "killed":
            continue
        if run.cleanup_completed_at is not None:
            continue

        from .lifecycle import _finalize_cleanup

        await _finalize_cleanup(run, "killed_finalization")
        count += 1

    return count


async def start_sweeper() -> None:
    """Start the sweeper background task if not already running."""
    global _sweeper_task
    if _sweeper_task is not None and not _sweeper_task.done():
        return

    _sweeper_task = asyncio.create_task(_sweep_loop())


async def stop_sweeper() -> None:
    """Stop the sweeper background task and wait for cancellation."""
    global _running, _sweeper_task, _backoff
    _running = False
    if _sweeper_task is not None:
        _sweeper_task.cancel()
        try:
            await _sweeper_task
        except asyncio.CancelledError:  # noqa: S110
            pass
        _sweeper_task = None
    # Fresh backoff state on next start (conservative direction).
    _backoff = None


_sweeper_task: asyncio.Task | None = None
_running = False
