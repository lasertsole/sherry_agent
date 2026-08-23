"""Kill sub-agent runs with control-scope checks, reconciliation snapshots, and cascade support."""

import asyncio
import time
from loguru import logger
from ..types.registry import SubagentRunRecord, ExecutionStatus, RunOutcome, RunOutcomeStatus
from ..types.capability import ControlScope
from ..registry import (
    get_run,
    set_run,
    cancel_task,
    all_runs,
    wake_yield_if_all_children_settled,
    save_kill_reconciliation,
)
from ..registry.queries import list_runs_for_requester
from ..registry.generation import get_latest_run_by_child_session_key
from .controller import can_control_run


def resolve_kill_target_state(run: SubagentRunRecord) -> str:
    """Determine if a run is 'killable', 'finalizing', or already 'terminal'."""
    if run.execution.status == ExecutionStatus.TERMINAL:
        return "terminal"
    if run.kill_reconciliation is not None and not run.kill_reconciliation.reconciled:
        # Kill reconciliation is in progress but not yet finalized
        return "finalizing"
    return "killable"


async def kill_subagent_run(
    run_id: str,
    reason: str = "killed",
    requester_session_key: str | None = None,
) -> SubagentRunRecord | None:
    """Kill a single sub-agent run: cancel its task, clear queues, and mark as KILLED."""
    run = get_run(run_id)
    if run is None:
        logger.warning("kill_subagent_run: run {} not found", run_id)
        return None

    target_state = resolve_kill_target_state(run)
    if target_state == "terminal":
        logger.debug("kill_subagent_run: run {} already terminal", run_id)
        return run
    if target_state == "finalizing":
        logger.warning("kill_subagent_run: run {} is finalizing, waiting before kill", run_id)
        await asyncio.sleep(1.0)  # Brief grace period for reconciliation to complete
        run = get_run(run_id)
        if run and run.execution.status == ExecutionStatus.TERMINAL:
            return run

    if requester_session_key:
        allowed, deny_reason = can_control_run(run, requester_session_key)
        if not allowed:
            logger.warning("kill_subagent_run: control denied for run {}: {}", run_id, deny_reason)
            return None

    from ..orphan.recovery import cancel_recovery
    cancel_recovery(run_id)

    save_kill_reconciliation(run_id)
    cancel_task(run_id)
    await _clear_session_queues(run.child_session_key)

    from ..registry.lifecycle import complete_subagent_run
    outcome = RunOutcome(status=RunOutcomeStatus.KILLED, error=reason)
    updated = await complete_subagent_run(run_id, outcome)
    if updated is None:
        logger.warning("kill_subagent_run: complete_subagent_run returned None for {}", run_id)
        return get_run(run_id)

    updated = updated.model_copy(update={"aborted_last_run": True})  # Flag for orphan recovery detection
    set_run(updated)

    logger.info("Killed subagent run {}: reason={}", run_id, reason)
    return updated


async def kill_subagent_run_with_cascade(
    run_id: str,
    reason: str = "killed",
    cascade: bool = True,
    requester_session_key: str | None = None,
) -> list[SubagentRunRecord]:
    """Kill a sub-agent run and optionally cascade to all its descendants."""
    killed: list[SubagentRunRecord] = []

    run = get_run(run_id)
    if run is None:
        return killed

    if cascade:
        seen_keys: set[str] = set()
        children = list_runs_for_requester(run.child_session_key)
        for child in children:
            if child.child_session_key in seen_keys:
                continue
            latest = get_latest_run_by_child_session_key(child.child_session_key)
            if latest is None or latest.run_id != child.run_id:
                continue  # Skip stale generations — only cascade-kill the latest generation
            seen_keys.add(child.child_session_key)

            if child.execution.status == ExecutionStatus.TERMINAL:
                continue

            allowed, _ = can_control_run(child, run.child_session_key)
            if not allowed:
                logger.debug("cascade kill: skipping child {} — no control permission", child.run_id)
                continue

            child_killed = await kill_subagent_run_with_cascade(
                child.run_id, reason=f"parent killed: {reason}", cascade=True,
                requester_session_key=child.controller_session_key,
            )
            killed.extend(child_killed)

    result = await kill_subagent_run(run_id, reason, requester_session_key)
    if result is not None:
        killed.append(result)

    if run and killed:
        try:
            await wake_yield_if_all_children_settled(run.requester_session_key)
        except Exception:
            pass

    return killed


def list_killable_children(session_key: str) -> list[SubagentRunRecord]:
    """List non-terminal child runs that are eligible for killing."""
    children = list_runs_for_requester(session_key)
    return [
        c for c in children
        if c.execution.status in (ExecutionStatus.RUNNING, ExecutionStatus.INTERRUPTED)
    ]


async def kill_subagent_run_admin(run_id: str, reason: str = "admin_kill") -> SubagentRunRecord | None:
    """Admin kill that bypasses requester-session-key checks."""
    return await kill_subagent_run(run_id, reason=reason)


async def kill_all_controlled_subagent_runs(
    requester_session_key: str,
    reason: str = "kill_all",
) -> list[SubagentRunRecord]:
    """Kill all killable children belonging to the given requester session."""
    killable = list_killable_children(requester_session_key)
    if not killable:
        return []

    first = get_run(killable[0].run_id)
    if first:
        allowed, deny_reason = can_control_run(first, requester_session_key)
        if not allowed:
            logger.warning("kill_all: control denied for {}: {}", requester_session_key, deny_reason)
            return []

    killed: list[SubagentRunRecord] = []
    for child in killable:
        result = await kill_subagent_run(
            child.run_id, reason=reason, requester_session_key=requester_session_key,
        )
        if result is not None:
            killed.append(result)
    if killed:
        try:
            await wake_yield_if_all_children_settled(requester_session_key)
        except Exception:
            pass
    return killed


async def _clear_session_queues(child_session_key: str) -> None:
    """Best-effort cancellation of pending asyncio tasks for a child session."""
    try:
        from ..registry import remove_task, get_task
        task = get_task(child_session_key)
        if task and not task.done():
            task.cancel()
    except Exception as e:
        logger.debug("Session queue clear skipped for {}: {}", child_session_key, e)
