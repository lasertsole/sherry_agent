"""Background sweeper daemon: periodically recovers orphans, retries suspended deliveries, detects stale runs, pressure-prunes, and persists to disk."""

import asyncio
import time
from loguru import logger
from ..config import get_config
from ..types.registry import ExecutionStatus


async def _sweep_loop() -> None:
    """Main sweeper loop: runs at the configured interval until stopped."""
    global _running
    _running = True
    config = get_config()
    interval = config.sweeper_interval_seconds

    logger.info("Subagent sweeper started (interval={}s)", interval)

    while _running:
        await asyncio.sleep(interval)
        try:
            await _do_sweep()
        except Exception as e:
            logger.error("Sweeper error: {}", e)


async def _do_sweep() -> None:
    """Execute one full sweep cycle: orphans, suspended deliveries, failed deliveries, pressure prune, and persist."""
    from .lifecycle import (
        recover_orphaned_runs,
        finalize_suspended_deliveries,
        finalize_failed_deliveries,
        pressure_prune_suspended_deliveries,
    )
    from .work_admission import run_with_work_admission
    from ..orphan.recovery import scan_orphaned_sessions, schedule_orphan_recovery, reclassify_legacy_timeout

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

    await run_with_work_admission(_persist_async(), label="sweeper-persist")


async def _expire_suspended_by_requester_type() -> int:
    """Expire suspended deliveries based on requester type (cron/subagent/interactive) with different TTLs."""
    from . import memory, mark_delivery_discarded, set_run

    _REQUESTER_TYPE_EXPIRY_MS = {
        "cron": 2 * 3600 * 1000,        # 2 hours for cron
        "subagent": 6 * 3600 * 1000,     # 6 hours for sub-agent
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
    from ..types.registry import RunOutcome, ExecutionState, RunOutcomeStatus

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
    global _running, _sweeper_task
    _running = False
    if _sweeper_task is not None:
        _sweeper_task.cancel()
        try:
            await _sweeper_task
        except asyncio.CancelledError:
            pass
        _sweeper_task = None


_sweeper_task: asyncio.Task | None = None
_running = False
