"""Registry initialization and persistence scheduling: restores from SQLite on startup and periodically flushes to disk."""

import asyncio
import time

from loguru import logger

from ..types.registry import ExecutionStatus, SubagentRunRecord
from . import memory
from . import store_sqlite


def get_config():
    """Lazy accessor: the config module imports this package (cycle-safe)."""
    from ..config import get_config as _get_config

    return _get_config()


_persist_lock = asyncio.Lock()
_restore_done = False


def _finalize_restored_pending(run: SubagentRunRecord) -> SubagentRunRecord:
    """Terminal shape for a restored PENDING run (delegates to the record)."""
    return run.finalize_pending_orphan()


def _rebase_restored_clock(run: SubagentRunRecord, now: float) -> SubagentRunRecord:
    """Move every persisted monotonic timestamp onto THIS process's clock.

    The shaping lives on :class:`SubagentRunRecord`; this wrapper exists so the
    restore path has one named seam.
    """
    return run.rebase_clock(now)


def _mark_restored_running_terminal(run: SubagentRunRecord, now: float) -> SubagentRunRecord:
    """Terminalize a RUNNING run restored from disk (delegates to the record)."""
    return run.terminate_on_restart(now)


async def persist_runs_to_disk() -> None:
    """Persist the full in-memory snapshot to SQLite."""
    runs = memory.snapshot()
    try:
        await store_sqlite.save_runs_to_sqlite(runs)
    except Exception as e:
        logger.exception("Failed to persist subagent runs to SQLite: {}", e)


async def restore_runs_from_disk() -> None:
    """Restore run records from SQLite into memory (once SUCCESSFULLY).

    The flag is set only after a successful pass: a transient SQLite failure at
    boot used to latch ``_restore_done`` before the read, so the restore never
    retried and every leftover run stayed invisible for the process lifetime.
    """
    global _restore_done
    if _restore_done:
        return

    try:
        runs = await store_sqlite.load_runs_from_sqlite()
        now = time.monotonic()
        restored_pending = 0
        restored_running = 0
        for run_id, run in runs.items():
            if memory.get(run_id) is not None:
                continue
            if run.execution.status == ExecutionStatus.PENDING:
                run = _finalize_restored_pending(run)
                restored_pending += 1
            elif run.execution.status == ExecutionStatus.RUNNING:
                run = _mark_restored_running_terminal(run, now)
                restored_running += 1
            run = _rebase_restored_clock(run, now)
            memory.set_run(run)
        logger.info("Restored {} subagent runs from SQLite", len(runs))
        if restored_pending:
            logger.info(
                "Finalized {} restored PENDING runs as pending_orphaned "
                "(no task survives a restart)",
                restored_pending,
            )
        if restored_running:
            logger.info(
                "Finalized {} restored RUNNING runs as process_restarted "
                "(no task survives a restart)",
                restored_running,
            )
        _restore_done = True
    except Exception as e:
        logger.exception("Failed to restore subagent runs from SQLite: {}", e)


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
            logger.exception("Periodic persist failed: {}", e)
