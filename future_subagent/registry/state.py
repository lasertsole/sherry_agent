"""Registry initialization and persistence scheduling: restores from SQLite on startup and periodically flushes to disk."""

import asyncio
from loguru import logger
from . import memory
from . import store_sqlite


_persist_lock = asyncio.Lock()
_restore_done = False


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
        for run_id, run in runs.items():
            existing = memory.get(run_id)
            if existing is None:
                memory.set_run(run)
        logger.info("Restored {} subagent runs from SQLite", len(runs))
    except Exception as e:
        logger.error("Failed to restore subagent runs from SQLite: {}", e)


async def init_registry() -> None:
    """Initialize the registry: create tables, restore from disk, and load settle-wake state. Call at service startup."""
    await store_sqlite.ensure_db()
    await restore_runs_from_disk()

    try:
        from .settle_wake import get_settle_wake_batch
        batch = get_settle_wake_batch()
        batch.load_persisted_state()
    except Exception as e:
        logger.debug("Failed to load settle-wake state: {}", e)

    logger.info("Subagent registry initialized")


async def periodic_persist(interval_seconds: int = 30) -> None:
    """Background loop that persists in-memory state to disk at a regular interval. Called by the sweeper."""
    while True:
        await asyncio.sleep(interval_seconds)
        try:
            await persist_runs_to_disk()
        except Exception as e:
            logger.error("Periodic persist failed: {}", e)
