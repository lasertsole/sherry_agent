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


def get_config():
    """Lazy accessor: the config module imports this package (cycle-safe)."""
    from ..config import get_config as _get_config

    return _get_config()


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


def _rebase_restored_clock(run: SubagentRunRecord, now: float) -> SubagentRunRecord:
    """Move every persisted monotonic timestamp onto THIS process's clock.

    ``time.monotonic()`` is process-relative: a value written by the previous
    process is meaningless here (a run that started 60 s before the restart can
    read as hours old, or as a future time), so each ``now - timestamp`` expiry
    check silently stops working. Rebasing to the restore point makes staleness
    and delivery expiry count from the restart — the longest a run can hide is
    one sweep interval after it. Durations (``accumulated_runtime_ms``) are not
    clocks and are preserved.
    """
    window = get_config().archive_after_minutes * 60

    def _at(value: float | None) -> float | None:
        return now if value is not None else None

    return run.model_copy(
        update={
            "execution": run.execution.model_copy(
                update={
                    "started_at": _at(run.execution.started_at),
                    "ended_at": _at(run.execution.ended_at),
                }
            ),
            "completion": run.completion.model_copy(
                update={"captured_at": _at(run.completion.captured_at)}
            ),
            "delivery": run.delivery.model_copy(
                update={
                    "last_attempt_at": _at(run.delivery.last_attempt_at),
                    "suspended_at": _at(run.delivery.suspended_at),
                    "delivered_at": _at(run.delivery.delivered_at),
                    "announced_at": _at(run.delivery.announced_at),
                }
            ),
            "kill_reconciliation": (
                run.kill_reconciliation.model_copy(update={"killed_at": now})
                if run.kill_reconciliation is not None
                else None
            ),
            # A DEADLINE, not a timestamp: rebasing it to "now" would archive
            # the run on the first sweep. Give it a fresh window instead.
            "archive_at": now + window if run.archive_at is not None else None,
            "cleanup_completed_at": _at(run.cleanup_completed_at),
        }
    )


def _mark_restored_running_terminal(run: SubagentRunRecord, now: float) -> SubagentRunRecord:
    """Terminalize a RUNNING run restored from disk: no task survives a restart.

    Leaving it RUNNING made every reader believe the child was alive — the
    WAITING-flow sweep never went stale, ``is_live_unended_run`` kept the link,
    and orphan recovery waited for a staleness threshold that a rebased clock
    had just reset. The run is reported as a TIMEOUT so its owner can re-dispatch
    (``pause_reason`` marks the cause); the delivery stays PENDING, which the
    sweeper's undelivered scan turns into a completion notice.
    """
    return run.model_copy(
        update={
            "execution": ExecutionState(
                status=ExecutionStatus.TERMINAL,
                started_at=run.execution.started_at,
                ended_at=now,
                outcome=RunOutcome(
                    status=RunOutcomeStatus.TIMEOUT, error="interrupted by a process restart"
                ),
            ),
            "pause_reason": "process_restart",
            "ended_reason": "process_restarted",
        }
    )


async def persist_runs_to_disk() -> None:
    """Persist the full in-memory snapshot to SQLite."""
    runs = memory.snapshot()
    try:
        await store_sqlite.save_runs_to_sqlite(runs)
    except Exception as e:
        logger.exception("Failed to persist subagent runs to SQLite: {}", e)


async def restore_runs_from_disk() -> None:
    """Restore run records from SQLite into memory (executes only once)."""
    global _restore_done
    if _restore_done:
        return
    _restore_done = True

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
