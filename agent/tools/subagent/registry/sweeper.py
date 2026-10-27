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

    # Unified orphan collection: the unreferenced-flow sweep first (mark active
    # todo roots, collect unmarked flows), then liveness (stuck-but-referenced),
    # then the zombie-todo pass inside liveness.
    unreferenced = await _unreferenced_flow_sweep()
    if unreferenced.get("swept"):
        logger.warning(
            "Sweeper: unreferenced-flow sweep collected {} TaskFlow(s) (sessions={}, roots={})",
            unreferenced["swept"],
            unreferenced["sessions"],
            unreferenced["roots"],
        )
    live = await _liveness_sweep()
    if live.get("session_dead") or live.get("stale_swept") or live.get("todo_cancelled"):
        logger.warning(
            "Sweeper: liveness collected {} dead-session + {} stale TaskFlow(s), "
            "cancelled {} zombie todo(s)",
            live["session_dead"],
            live["stale_swept"],
            live["todo_cancelled"],
        )

    stale_waiting = await _scan_stale_waiting_taskflows()
    if stale_waiting > 0:
        logger.warning("Sweeper: {} TaskFlow(s) in stale WAITING state", stale_waiting)

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
                state = dict(flow.get("state") or {})
                state["failure_reason"] = "Deadline exceeded: " + time.strftime(
                    "%Y-%m-%d %H:%M", time.localtime(deadline_ts)
                )
                await taskflow_store.update_flow(
                    flow["flow_id"],
                    flow["expected_revision"],
                    session_id=flow["session_id"],
                    state=state,
                    wait=None,
                    status=TaskFlowStatus.FAILED.value,
                    terminal_reason="deadline_exceeded",
                )
                expired += 1
                logger.warning("TaskFlow '{}' deadline exceeded, marked failed", flow["flow_id"])
            except Exception as e:
                logger.warning("Failed to expire TaskFlow '{}': {}", flow["flow_id"], e)
        return expired
    except Exception as e:
        logger.warning("TaskFlow deadline sweep failed: {}", e)
        return 0


def _child_session_keys(flow: dict) -> list[str]:
    """Every child session bound to a flow (row-level + per-step)."""
    keys: list[str] = []
    row_key = str(flow.get("child_session_key") or "").strip()
    if row_key:
        keys.append(row_key)
    for step in (flow.get("state") or {}).get("steps") or []:
        if not isinstance(step, dict):
            continue
        key = str(step.get("child_session_key") or "").strip()
        if key and key not in keys:
            keys.append(key)
    return keys


def _has_live_child(flow: dict) -> bool:
    """True when a child may still finish and resume this flow.

    A failed lookup is treated as LIVE (veto): the sweeper must never collect a
    flow whose child liveness is unknown.
    """
    try:
        from agent.tools.subagent.registry.helpers import is_live_unended_run
        from agent.tools.subagent.registry.queries import get_run_by_child_session_key
    except Exception:  # noqa: BLE001 - optional subsystem
        return True
    for key in _child_session_keys(flow):
        try:
            run = get_run_by_child_session_key(key)
            if run is not None and is_live_unended_run(run):
                return True
        except Exception:  # noqa: BLE001 - unknown is live
            return True
    return False


def _is_child_session(session_id: str) -> bool:
    """A subagent/swarm session key (never an independent main session)."""
    value = str(session_id or "")
    return value.startswith("agent:") and (":subagent:" in value or ":swarm:" in value)


def _session_has_live_presence(session_id: str) -> bool:
    """Conservative evidence that a session is alive: socket, turn, or live run."""
    try:
        from runtime.session.relation_register import relation_register

        if relation_register.get_websockets_by_session_id(session_id):
            return True
    except Exception:  # noqa: BLE001 - an unreadable signal is treated as live
        return True
    try:
        from agent.tools.subagent.registry.session_state import is_session_busy

        if is_session_busy(session_id):
            return True
    except Exception:  # noqa: BLE001
        return True
    try:
        from agent.tools.subagent.registry.queries import has_live_run_for_session

        if has_live_run_for_session(session_id):
            return True
    except Exception:  # noqa: BLE001
        return True
    return False


async def _unreferenced_flow_sweep(
    *,
    now: float | None = None,
    grace_seconds: float | None = None,
    session_ids: set[str] | None = None,
) -> dict[str, int]:
    """Collect non-terminal flows that no active todo references.

    This is a mark-and-sweep reachability pass. GC roots
    are active todos (``pending``/``in_progress``) carrying a ``flow_id``; a
    root's flow is marked reachable. A non-terminal, non-superseded flow older
    than the grace window and absent from the marked set is failed as
    ``orphan_unreferenced``. The grace window covers the create/bind race across
    the two SQLite files. ``session_ids`` narrows the scan for an immediate
    post-replacement pass; None keeps the system-wide sweep.
    """
    from config.features import TASKFLOW_INFRA
    from agent.tools.taskflow.registry import store_sqlite as taskflow_store
    from agent.tools.todolist.service import TodoService

    current = time.time() if now is None else float(now)
    grace = (
        float(TASKFLOW_INFRA["unreferenced_flow_grace_seconds"])
        if grace_seconds is None
        else float(grace_seconds)
    )
    cutoff = current - grace
    try:
        if session_ids is None:
            sessions = await taskflow_store.get_non_terminal_session_ids()
        else:
            sessions = sorted(
                str(session_id) for session_id in session_ids if str(session_id or "").strip()
            )
    except Exception as exc:  # noqa: BLE001 - a sweep read failure must not stop the loop
        logger.warning("Unreferenced-flow sweep could not read non-terminal sessions: {}", exc)
        return {"sessions": 0, "roots": 0, "swept": 0}

    roots_total = 0
    swept_total = 0
    for session_id in sessions:
        try:
            root_ids = await TodoService.get_active_flow_ids(session_id)
            roots_total += len(root_ids)
            for flow in await taskflow_store.get_non_terminal_flows(session_id):
                if str(flow.get("flow_id") or "") in root_ids:
                    continue
                updated_at = flow.get("updated_at")
                if updated_at is not None and float(updated_at) >= cutoff:
                    continue
                # A root can be deleted while its child still runs (for example
                # a full todowrite replacement that drops it). The flow is then
                # unreferenced but NOT dead; liveness owns it once the child
                # settles, so this pass must not collect it here.
                if _has_live_child(flow):
                    continue
                if await taskflow_store.fail_flow_for_gc(flow, reason="orphan_unreferenced"):
                    swept_total += 1
        except Exception as exc:  # noqa: BLE001 - per-session fail-open
            logger.warning("Unreferenced-flow sweep skipped session {}: {}", session_id, exc)
    return {"sessions": len(sessions), "roots": roots_total, "swept": swept_total}


async def _liveness_sweep(
    *,
    now: float | None = None,
    active_session_ids: set[str] | None = None,
    stale_action_hours: float | None = None,
) -> dict[str, int]:
    """Collect stuck-but-referenced flows and their zombie todos.

    Session-level liveness is deliberately CONSERVATIVE: a flow is only failed
    when it is ALSO past the inactivity cutoff. This cannot mass-fail every
    flow at boot (no sockets exist yet); a recently-created or recently-used
    flow always survives. ``active_session_ids`` is injectable for tests; the
    production path derives it from live sockets, turns and subagent runs.
    """
    from config.features import TASKFLOW_INFRA
    from agent.tools.taskflow.registry import store_sqlite as taskflow_store
    from agent.tools.todolist.service import TodoService

    current = time.time() if now is None else float(now)
    hours = (
        float(TASKFLOW_INFRA["stale_action_hours"])
        if stale_action_hours is None
        else float(stale_action_hours)
    )
    child_grace = float(TASKFLOW_INFRA["unreferenced_flow_grace_seconds"])
    try:
        flows = await taskflow_store.get_non_terminal_flows()
        sessions = {str(flow.get("session_id") or "") for flow in flows}
        sessions.update(await taskflow_store.get_failed_flow_session_ids())
        sessions.discard("")
    except Exception as exc:  # noqa: BLE001 - fail-open
        logger.warning("Liveness sweep could not read TaskFlow state: {}", exc)
        return {"session_dead": 0, "stale_swept": 0, "todo_cancelled": 0}

    active = (
        {sid for sid in sessions if _session_has_live_presence(sid)}
        if active_session_ids is None
        else set(active_session_ids)
    )
    session_dead = 0
    stale_swept = 0
    for flow in flows:
        session_id = str(flow.get("session_id") or "")
        if not session_id or _has_live_child(flow):
            continue
        updated_at = flow.get("updated_at")
        if updated_at is None:
            updated_at = 0.0
        age = current - float(updated_at)
        if _is_child_session(session_id):
            # A child session has no independent life after its run ends. It
            # gets the short orphan grace (not the 12h inactivity window),
            # so a killed child's flow is collected promptly — but a live run
            # still vetoes through the active set / live-run check.
            child_live = (
                session_id in active
                if active_session_ids is not None
                else _session_has_live_presence(session_id)
            )
            if child_live or age < child_grace:
                continue
            reason = "session_dead"
        else:
            if age < hours * 3600:
                continue
            reason = "stale_swept" if session_id in active else "session_dead"
        try:
            if await taskflow_store.fail_stale_flow(flow, reason=reason):
                if reason == "session_dead":
                    session_dead += 1
                else:
                    stale_swept += 1
        except Exception as exc:  # noqa: BLE001
            logger.debug("Liveness finalize skipped for {}: {}", flow.get("flow_id"), exc)

    todo_cancelled = 0
    for session_id in sessions:
        try:
            zombie_flows = set(await taskflow_store.get_zombie_flow_ids(session_id))
            if not zombie_flows:
                continue
            roots = await TodoService.get_active_flow_ids(session_id)
            referenced = sorted(zombie_flows & roots)
            if referenced:
                todo_cancelled += await TodoService.cancel_todos_for_flows(session_id, referenced)
        except Exception as exc:  # noqa: BLE001
            logger.debug("Zombie-todo sweep skipped for {}: {}", session_id, exc)
    return {
        "session_dead": session_dead,
        "stale_swept": stale_swept,
        "todo_cancelled": todo_cancelled,
    }


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

    # Boot is exactly when a crash's TaskFlow leftovers are around. Run the
    # unreferenced-flow and liveness passes NOW instead of waiting one interval;
    # both are conservative (grace + inactivity), so a flow from a
    # closed-but-valid session is not collected just because no socket exists yet.
    try:
        unreferenced = await _unreferenced_flow_sweep()
        live = await _liveness_sweep()
        if unreferenced.get("swept") or live.get("session_dead") or live.get("stale_swept"):
            logger.warning("Boot TaskFlow GC: unreferenced-flow={} liveness={}", unreferenced, live)
    except Exception:  # noqa: BLE001 - boot must not fail over a cleanup scan
        logger.debug("boot TaskFlow GC skipped", exc_info=True)

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
