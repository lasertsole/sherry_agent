"""Periodic followup loop that detects timed-out sub-agents and triggers orphan recovery."""

import asyncio
from loguru import logger
from ..registry import all_runs, is_live_unended_run
from ..registry.lifecycle import recover_orphaned_runs
from ..config import get_config

# Global followup task reference
_followup_task: asyncio.Task | None = None
_running = False


async def _followup_loop() -> None:
    """Main followup loop: periodically check for timed-out runs and recover them."""
    global _running
    _running = True
    config = get_config()
    # Followup interval is 2× sweeper interval to avoid being too aggressive
    interval = config.sweeper_interval_seconds * 2  # 2× sweeper interval to avoid being too aggressive

    logger.info("Subagent followup started (interval={}s)", interval)

    while _running:
        await asyncio.sleep(interval)
        try:
            await _check_timeouts()
        except Exception as e:
            logger.error("Followup error: {}", e)


async def _check_timeouts() -> None:
    """Scan all live runs for timeout and trigger batch orphan recovery if any found."""
    config = get_config()
    import time

    timed_out = 0
    for run in all_runs():
        if not is_live_unended_run(run):
            continue
        if run.execution.started_at is None:
            continue

        elapsed = time.monotonic() - run.execution.started_at
        if elapsed > config.run_timeout_seconds:
            logger.warning("Followup: run {} exceeded timeout ({:.0f}s > {:.0f}s)",
                          run.run_id, elapsed, config.run_timeout_seconds)
            timed_out += 1

    # Trigger batch recovery when timed-out runs are detected
    if timed_out > 0:
        recovered = await recover_orphaned_runs()
        logger.info("Followup recovered {} timed-out runs", recovered)


async def start_followup() -> None:
    """Start the followup periodic task (idempotent: no-op if already running)."""
    global _followup_task
    if _followup_task is not None and not _followup_task.done():
        return

    _followup_task = asyncio.create_task(_followup_loop())


async def stop_followup() -> None:
    """Stop the followup periodic task and wait for cancellation."""
    global _running, _followup_task
    _running = False
    if _followup_task is not None:
        _followup_task.cancel()
        try:
            await _followup_task
        except asyncio.CancelledError:
            pass
        _followup_task = None
