"""Completion state resolution: determines run outcomes and maps them to lifecycle ended reasons."""

import time
from loguru import logger
from ..types.registry import SubagentRunRecord, RunOutcome, ExecutionState
from ..types.registry import RunOutcomeStatus
from ..types.lifecycle import LifecycleEndedReason, outcome_to_ended_reason


def should_update_run_outcome(current: RunOutcome | None, new_outcome: RunOutcome) -> bool:
    """Decide whether a new outcome should replace the current one.

    Allows updates from UNKNOWN→known, OK→ERROR/TIMEOUT, but not downgrades.
    """
    if current is None:
        return True
    if current.status == RunOutcomeStatus.UNKNOWN and new_outcome.status != RunOutcomeStatus.UNKNOWN:
        return True
    if current.status == RunOutcomeStatus.OK and new_outcome.status in (RunOutcomeStatus.ERROR, RunOutcomeStatus.TIMEOUT):
        return True
    return False


def resolve_finalized_task_state(run: SubagentRunRecord) -> dict:
    """Derive ended_reason, terminal_state, and summaries from a completed run's outcome."""
    outcome = run.execution.outcome
    if outcome is None:
        return {
            "ended_reason": LifecycleEndedReason.ORPHANED,
            "terminal_state": "failed",
            "progress_summary": None,
            "terminal_summary": run.completion.result_text or "no outcome recorded",
        }

    ended_reason = outcome_to_ended_reason(outcome.status)
    terminal_state_map = {
        RunOutcomeStatus.OK: "succeeded",
        RunOutcomeStatus.ERROR: "failed",
        RunOutcomeStatus.TIMEOUT: "timed_out",
        RunOutcomeStatus.KILLED: "cancelled",
    }

    return {
        "ended_reason": ended_reason,
        "terminal_state": terminal_state_map.get(outcome.status, "failed"),
        "progress_summary": run.completion.result_text[:200] if run.completion.result_text else None,
        "terminal_summary": run.completion.result_text or outcome.error or "no result",
    }


def resolve_lifecycle_outcome(run: SubagentRunRecord) -> RunOutcomeStatus:
    """Return the outcome status of a run, or UNKNOWN if not yet resolved."""
    if run.execution.outcome is not None:
        return run.execution.outcome.status
    return RunOutcomeStatus.UNKNOWN


_ended_hook_in_flight: set[str] = set()


async def emit_ended_hook_once(run: SubagentRunRecord) -> None:
    """Fire the stop-hook exactly once per run; guarded against concurrent invocation."""
    if run.ended_hook_emitted:
        return
    if run.run_id in _ended_hook_in_flight:
        return

    _ended_hook_in_flight.add(run.run_id)
    try:
        from ..hooks.base import fire_stop_hooks
        await fire_stop_hooks(run)
        from .memory import update as update_run
        update_run(run.run_id, ended_hook_emitted=True)
    except Exception as e:
        logger.error("emit_ended_hook_once failed for run {}: {}", run.run_id, e)
    finally:
        _ended_hook_in_flight.discard(run.run_id)
