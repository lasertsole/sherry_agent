"""Registry initialization and persistence scheduling: restores from SQLite on startup and periodically flushes to disk."""

import asyncio
import time

from loguru import logger

from ..types.registry import (
    ExecutionState,
    ExecutionStatus,
    RunOutcome,
    RunOutcomeStatus,
    SubagentRunRecord,
)
from . import memory
from . import store_sqlite


_persist_lock = asyncio.Lock()
_restore_done = False


def _finalize_restored_pending(run: SubagentRunRecord) -> SubagentRunRecord:
    """Terminal shape for a restored PENDING run, mirroring orphan/recovery.py.

    A PENDING run never started, so the lane-waiting task died with the
    previous process and can never be promoted again. Finalizing at restore
    time keeps the run visible in queries without letting it trickle through
    the sweeper's orphan recovery, which announces every leftover run one by
    one — the restart-time "Error: pending orphaned" console flood.
    """
    return run.model_copy(
        update={
            "execution": ExecutionState(
                status=ExecutionStatus.TERMINAL,
                started_at=None,
                ended_at=time.monotonic(),
                outcome=RunOutcome(status=RunOutcomeStatus.TIMEOUT, error="pending orphaned"),
            ),
            "ended_reason": "pending_orphaned",
        }
    )


async def persist_runs_to_disk() -> None:
    """Persist the full in-memory snapshot to SQLite."""
    runs = memory.snapshot()
    try:
        await store_sqlite.save_runs_to_sqlite(runs)
    except Exception as e:
        logger.error("Failed to persist subagent runs to SQLite: {}", e)


async def restore_runs_from_disk() -> None:
    """Restore run records from SQLite into memory (executes only once)."""
    global _restore_done
    if _restore_done:
        return
    _restore_done = True

    try:
        runs = await store_sqlite.load_runs_from_sqlite()
        restored_pending = 0
        for run_id, run in runs.items():
            if memory.get(run_id) is not None:
                continue
            if run.execution.status == ExecutionStatus.PENDING:
                run = _finalize_restored_pending(run)
                restored_pending += 1
            memory.set_run(run)
        logger.info("Restored {} subagent runs from SQLite", len(runs))
        if restored_pending:
            logger.info(
                "Finalized {} restored PENDING runs as pending_orphaned "
                "(no task survives a restart)",
                restored_pending,
            )
    except Exception as e:
        logger.error("Failed to restore subagent runs from SQLite: {}", e)


async def init_registry() -> None:
    """Initialize the registry: create tables, restore from disk, load settle-wake state, and start EventBus bridge. Call at service startup."""
    await store_sqlite.ensure_db()
    await restore_runs_from_disk()

    try:
        from .settle_wake import get_settle_wake_batch

        batch = get_settle_wake_batch()
        batch.load_persisted_state()
    except Exception as e:
        logger.debug("Failed to load settle-wake state: {}", e)

    try:
        from ..events.bridge import start_bridge

        start_bridge()
    except Exception as e:
        logger.warning("Failed to start EventBus bridge: {}", e)

    logger.info("Subagent registry initialized")


async def periodic_persist(interval_seconds: int = 30) -> None:
    """Background loop that persists in-memory state to disk at a regular interval. Called by the sweeper."""
    while True:
        await asyncio.sleep(interval_seconds)
        try:
            await persist_runs_to_disk()
        except Exception as e:
            logger.error("Periodic persist failed: {}", e)
