"""Background sweeper daemon: periodically recovers orphans, retries suspended deliveries, detects stale runs, pressure-prunes, and persists to disk."""

# allow: SIZE_OK — every scanner shares one sweep loop, one backoff, and one
# persist; forking the TaskFlow scans into another module would split the
# lifecycle they are driven by.

import asyncio
import time
from loguru import logger
from runtime.process.periodic_backoff import PeriodicBackoff
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

    stale_running = await _sweep_stale_running_taskflows()
    if stale_running > 0:
        logger.warning(
            "Sweeper: {} TaskFlow(s) had no activity past the stale threshold, marked failed",
            stale_running,
        )

    stale_waiting = await _scan_stale_waiting_taskflows()
    if stale_waiting > 0:
        logger.warning("Sweeper: {} TaskFlow(s) in stale WAITING state", stale_waiting)

    orphaned_flows = await _reconcile_orphaned_taskflows()
    if orphaned_flows > 0:
        logger.warning(
            "Sweeper: {} orphaned TaskFlow(s) (owner session gone) marked cancelled",
            orphaned_flows,
        )

    purged = await _reconcile_purge_pending()
    if purged > 0:
        logger.info("Sweeper: replayed {} pending TaskFlow purge(s)", purged)

    await run_with_work_admission(_persist_async(), label="sweeper-persist")

    # Isolated workspaces are not registry rows: a crash between their creation
    # and the run's registration leaves a directory + worktree + branch that no
    # other scanner can see. Same cycle, its own scan.
    from ..isolation.sweep import sweep_stale_isolated_workspaces

    stale_workspaces = sweep_stale_isolated_workspaces()
    if stale_workspaces > 0:
        logger.info("Sweeper cleaned {} stale isolated workspace(s)", stale_workspaces)


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
                reason = "Deadline exceeded: " + time.strftime(
                    "%Y-%m-%d %H:%M", time.localtime(deadline_ts)
                )
                state = dict(flow.get("state") or {})
                state["failure_reason"] = reason
                _fail_unresolved_steps(state, reason)
                await taskflow_store.update_flow(
                    flow["flow_id"],
                    flow["expected_revision"],
                    session_id=flow["session_id"],
                    state=state,
                    status=TaskFlowStatus.FAILED.value,
                    terminal_reason="deadline_exceeded",
                    wait=None,
                )
                expired += 1
                logger.warning("TaskFlow '{}' deadline exceeded, marked failed", flow["flow_id"])
            except Exception as e:
                logger.warning("Failed to expire TaskFlow '{}': {}", flow["flow_id"], e)
        return expired
    except Exception as e:
        logger.warning("TaskFlow deadline sweep failed: {}", e)
        return 0


def _fail_unresolved_steps(state: dict, reason: str) -> None:
    """Mark non-terminal steps failed so a failed flow has no ``dispatched`` ghosts."""
    steps = state.get("steps") or []
    for step in steps:
        if not isinstance(step, dict):
            continue
        status = str(step.get("status") or "")
        if status in ("done", "failed", "skipped", "cancelled"):
            continue
        step["status"] = "failed"
        step["fail_reason"] = reason
    state["steps"] = steps


async def _sweep_stale_running_taskflows() -> int:
    """Fail RUNNING/WAITING flows with no activity past the stale threshold.

    A live child vetoes the failure (a legitimate long-running child may not
    touch the flow until it finishes). An unavailable liveness source also
    vetoes (cannot prove the child is dead). ``stale_action_hours=0`` disables
    the pass. This is the action tier that catches a flow whose owner session
    still exists but whose LLM walked away.
    """
    from config.features import TASKFLOW_INFRA
    from agent.tools.taskflow.config import TaskFlowStatus
    from agent.tools.taskflow.reconcile import child_session_keys
    from agent.tools.taskflow.registry import store_sqlite as taskflow_store

    hours = float(TASKFLOW_INFRA["stale_action_hours"])
    if hours <= 0:
        return 0
    try:
        from agent.tools.subagent.registry.helpers import is_live_unended_run
        from agent.tools.subagent.registry.queries import get_run_by_child_session_key
    except Exception:  # noqa: BLE001 - cannot prove a child dead -> do nothing
        return 0

    try:
        threshold = time.time() - hours * 3600
        candidates = await taskflow_store.get_stale_candidates(
            statuses=(TaskFlowStatus.RUNNING.value, TaskFlowStatus.WAITING.value),
            action_before=threshold,
        )
    except Exception as e:  # noqa: BLE001 - lifecycle sweeps are fail-open
        logger.warning("TaskFlow stale-running sweep failed to read the store: {}", e)
        return 0

    failed = 0
    for flow in candidates:
        try:
            keys = child_session_keys(flow)
            live = False
            for key in keys:
                run = get_run_by_child_session_key(key)
                if run is not None and is_live_unended_run(run):
                    live = True
                    break
            if live:
                continue
        except Exception:
            # A lookup failure means UNKNOWN: never fail a flow whose child may run.
            continue

        reason = f"stale: no activity for more than {hours:.0f}h"
        state = dict(flow.get("state") or {})
        state["failure_reason"] = reason
        _fail_unresolved_steps(state, reason)
        try:
            await taskflow_store.update_flow(
                flow["flow_id"],
                flow["expected_revision"],
                session_id=flow["session_id"],
                state=state,
                status=TaskFlowStatus.FAILED.value,
                terminal_reason="stale_swept",
                wait=None,
            )
        except Exception as e:  # noqa: BLE001
            logger.debug("TaskFlow stale sweep skipped {}: {}", flow["flow_id"], e)
            continue
        failed += 1
        logger.warning(
            "TaskFlow '{}' had no activity for > {:.0f}h; marked failed (stale_swept)",
            flow["flow_id"],
            hours,
        )
    return failed


async def _reconcile_orphaned_taskflows() -> int:
    """Cancel flows whose owner session is gone and whose children are settled.

    The reconciler owns the conservative conditions (session proof, live-child
    veto, grace window); this wrapper only isolates the sweeper from its
    failures — an unavailable store or hook means "do nothing", never a crash.
    """
    try:
        from agent.tools.taskflow.reconcile import reconcile_orphaned_flows

        return len(await reconcile_orphaned_flows())
    except Exception as exc:  # noqa: BLE001 - lifecycle sweeps are fail-open
        logger.debug("TaskFlow orphan reconciliation skipped: {}", exc)
        return 0


async def _reconcile_purge_pending() -> int:
    """Replay a session purge's durable intent bits (idempotent, every tick).

    ``todos.db`` and ``taskflow_registry.db`` cannot share a transaction, so a
    failed half of ``purge_session`` leaves ``purge_pending`` set. Each replay
    repeats the delete/clear operation; success clears the bit, a second run is
    a no-op. Any failure leaves the bit for the next tick.
    """
    from agent.tools.taskflow.config import (
        PURGE_BIT_DANGLING_TODO_PTR,
        PURGE_BIT_FLOWS_NOT_DELETED,
        PURGE_BIT_TODOS_NOT_CLEARED,
    )
    from agent.tools.taskflow.registry import store_sqlite as taskflow_store
    from agent.tools.todolist.registry import store_sqlite as todo_store

    try:
        pending = await taskflow_store.get_flows_with_purge_pending()
    except Exception as exc:  # noqa: BLE001 - lifecycle sweeps are fail-open
        logger.warning("TaskFlow purge replay could not read the store: {}", exc)
        return 0

    replayed = 0
    for flow in pending:
        flow_id = str(flow.get("flow_id") or "")
        session_id = str(flow.get("session_id") or "")
        bits = int(flow.get("purge_pending") or 0)
        if not flow_id or not session_id:
            continue
        if bits & PURGE_BIT_TODOS_NOT_CLEARED:
            try:
                await todo_store.delete_todos_by_session(session_id)
                await taskflow_store.clear_purge_bit_for_flow(
                    flow_id, session_id, PURGE_BIT_TODOS_NOT_CLEARED
                )
            except Exception as exc:  # noqa: BLE001
                logger.debug("TaskFlow purge replay (todos) failed for {}: {}", flow_id, exc)
        if bits & PURGE_BIT_FLOWS_NOT_DELETED:
            try:
                await taskflow_store.delete_flows_by_session(session_id)
                await taskflow_store.clear_purge_bit_for_flow(
                    flow_id, session_id, PURGE_BIT_FLOWS_NOT_DELETED
                )
            except Exception as exc:  # noqa: BLE001
                logger.debug("TaskFlow purge replay (flows) failed for {}: {}", flow_id, exc)
        if bits & PURGE_BIT_DANGLING_TODO_PTR:
            try:
                linked = await todo_store.get_todos_by_flow(session_id, flow_id)
                for todo in linked:
                    await todo_store.clear_flow_link(session_id, int(todo["position"]))
                await taskflow_store.clear_purge_bit_for_flow(
                    flow_id, session_id, PURGE_BIT_DANGLING_TODO_PTR
                )
            except Exception as exc:  # noqa: BLE001
                logger.debug("TaskFlow purge replay (todo link) failed for {}: {}", flow_id, exc)
        replayed += 1
    return replayed


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

            # A flow can bind children at the row level (taskflow_run_task) OR
            # only on its steps (taskflow_dispatch deliberately leaves the row
            # key untouched). Inspect BOTH; any live child keeps the flow out of
            # the stale marker.
            child_keys: list[str] = []
            row_key = str(flow.get("child_session_key") or "").strip()
            if row_key:
                child_keys.append(row_key)
            for step in (flow.get("state") or {}).get("steps") or []:
                if not isinstance(step, dict):
                    continue
                key = str(step.get("child_session_key") or "").strip()
                if key and key not in child_keys:
                    child_keys.append(key)

            child_live = False
            for child_key in child_keys:
                try:
                    from agent.tools.subagent.registry.queries import get_run_by_child_session_key
                    from agent.tools.subagent.registry.helpers import is_live_unended_run

                    run = get_run_by_child_session_key(child_key)
                    if run is not None and is_live_unended_run(run):
                        child_live = True
                        break
                except Exception:
                    # Liveness cannot be verified -> treat that child as dead.
                    continue

            if child_live:
                logger.debug(
                    "TaskFlow '{}' waiting {:.1f}h but child session is still live",
                    flow["flow_id"],
                    waiting_secs / 3600,
                )
                continue

            stale_count += 1
            logger.warning(
                "TaskFlow '{}' in stale WAITING ({:.1f}h, children={}): "
                "child session appears inactive; consider resume or re-dispatch",
                flow["flow_id"],
                waiting_secs / 3600,
                child_keys,
            )

            # Non-destructive marker: refresh the wait payload in place.
            try:
                wait["stale_detected_at"] = now
                wait["stale_child_session_key"] = child_keys[0] if child_keys else None
                await taskflow_store.update_flow(
                    flow["flow_id"],
                    flow["expected_revision"],
                    session_id=flow["session_id"],
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
    """Start the sweeper background task if not already running.

    The first registry cycle waits one interval; the stale-workspace scan is
    cheap and boot is exactly when a crash's leftovers are still around, so it
    runs immediately here (best-effort, in a worker thread: the scan does disk
    and git I/O and must not block startup).
    """
    global _sweeper_task
    if _sweeper_task is not None and not _sweeper_task.done():
        return

    try:
        from ..isolation.sweep import sweep_stale_isolated_workspaces

        cleaned = await asyncio.to_thread(sweep_stale_isolated_workspaces)
        if cleaned:
            logger.info("Boot: cleaned {} stale isolated workspace(s)", cleaned)
    except Exception:
        logger.debug("boot stale-workspace sweep skipped", exc_info=True)

    # Startup is the moment crash leftovers are around: run the TaskFlow owner
    # scan and the stale-running action once now instead of waiting a cycle.
    reconciled = await _reconcile_orphaned_taskflows()
    if reconciled:
        logger.warning("Boot: reconciled {} orphaned TaskFlow(s)", reconciled)
    stale = await _sweep_stale_running_taskflows()
    if stale:
        logger.warning("Boot: failed {} stale TaskFlow(s)", stale)

    _sweeper_task = asyncio.create_task(_sweep_loop())


async def stop_sweeper() -> None:
    """Stop the sweeper background task, wait for cancellation, and cancel pending deferred-cleanup timers."""
    global _running, _sweeper_task, _backoff
    _running = False
    if _sweeper_task is not None:
        _sweeper_task.cancel()
        try:
            await _sweeper_task
        except asyncio.CancelledError:  # noqa: S110
            pass
        _sweeper_task = None
    # Deferred-cleanup timers are driven by the sweeper's lifecycle: cancel
    # them together with it so no timer outlives the stopped subsystem.
    from .lifecycle import shutdown_deferred_cleanup

    await shutdown_deferred_cleanup()
    # Fresh backoff state on next start (conservative direction).
    _backoff = None


_sweeper_task: asyncio.Task | None = None
_running = False
