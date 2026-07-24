"""Delayed recovery for orphaned sub-agents.

Detects aborted-last-run flags, recovers chat history for resume messages,
evaluates recovery gates (recoverable / wedged / aborted), and retries
finalization of interrupted runs.
"""

import asyncio
import time
from loguru import logger
from ..types.registry import SubagentRunRecord, ExecutionStatus, RunOutcome, RunOutcomeStatus
from ..types.capability import ControlScope
from ..registry import (
    get_run,
    set_run,
    all_runs,
    is_live_unended_run,
    replace_run_after_steer,
)
from ..registry.helpers import is_stale_unended_run, reconcile_orphaned_run
from ..registry.lifecycle import complete_subagent_run
from ..announce.core import run_subagent_announce_flow
from ..config import get_config

_recovery_tasks: dict[str, asyncio.Task] = {}
_MAX_RECOVERY_ATTEMPTS = 3
_WEDGED_AGE_SECONDS = 86400  # 24 hours — runs older than this are considered permanently stuck
recovery_attempts_persisted: dict[str, int] = {}


async def schedule_orphan_recovery(run_id: str, delay_seconds: float | None = None) -> None:
    """Schedule a delayed recovery attempt for an orphaned run, respecting max attempts."""
    config = get_config()
    if delay_seconds is None:
        delay_seconds = config.orphan_recovery_delay_seconds

    if run_id in _recovery_tasks and not _recovery_tasks[run_id].done():
        return

    attempts = recovery_attempts_persisted.get(run_id, 0)
    if attempts >= _MAX_RECOVERY_ATTEMPTS:
        logger.warning("Max recovery attempts reached for run {}, giving up", run_id)
        return

    task = asyncio.create_task(_recovery_loop(run_id, delay_seconds))
    _recovery_tasks[run_id] = task


async def _recovery_loop(run_id: str, delay_seconds: float) -> None:
    """Wait, then evaluate the recovery gate and either resume or finalize the run."""
    await asyncio.sleep(delay_seconds)

    run = get_run(run_id)
    if run is None:
        return

    if not is_live_unended_run(run):
        return

    gate_result = evaluate_recovery_gate(run)
    if gate_result == "wedged":
        logger.warning("Run {} is wedged, forcing terminal", run_id)
        from ..registry.memory import update as update_run
        update_run(run_id, ended_reason="wedged_recovery")
        updated = reconcile_orphaned_run(run)
        if updated is not None:
            set_run(updated)
            await run_subagent_announce_flow(updated)
        _recovery_tasks.pop(run_id, None)
        return

    if gate_result == "aborted_last_run":
        logger.info("Run {} has abortedLastRun flag, attempting resume", run_id)

    recovery_attempts_persisted[run_id] = recovery_attempts_persisted.get(run_id, 0) + 1
    _persist_recovery_attempt(run_id)

    logger.info("Attempting orphan recovery for run {} (attempt {})", run_id, recovery_attempts_persisted.get(run_id, 0))

    if await _attempt_resume(run):
        logger.info("Orphan recovery resume succeeded for run {}", run_id)
    else:
        updated = await finalize_interrupted_run_with_retry(run.run_id)

    _recovery_tasks.pop(run_id, None)


def evaluate_recovery_gate(run: SubagentRunRecord) -> str:
    """Classify a run as 'wedged', 'aborted_last_run', or 'recoverable'."""
    if run.execution.started_at is not None:
        age = time.monotonic() - run.execution.started_at
        if age > _WEDGED_AGE_SECONDS:
            return "wedged"

    attempts = recovery_attempts_persisted.get(run.run_id, 0)
    if run.recovery_attempts_persisted > _MAX_RECOVERY_ATTEMPTS:
        return "wedged"

    if run.aborted_last_run:
        return "aborted_last_run"

    return "recoverable"


async def scan_orphaned_sessions() -> list[SubagentRunRecord]:
    """Scan all runs and return those that are live but have no active task (orphaned)."""
    from ..registry import get_task
    orphans = []

    for run in all_runs():
        if not is_live_unended_run(run):
            continue

        if run.aborted_last_run:
            orphans.append(run)
            continue

        task = get_task(run.run_id)
        if task is None or task.done():
            orphans.append(run)

    return orphans


async def _attempt_resume(run: SubagentRunRecord) -> bool:
    """Try to resume an orphaned run by steering it with a recovery message."""
    try:
        resume_message = await _build_resume_message(run)
        if not resume_message:
            return False

        updated = replace_run_after_steer(run.run_id, new_task=run.task)
        if updated is None:
            return False

        from ..control.steer import steer_subagent_run
        result = await steer_subagent_run(run.run_id, new_instructions=resume_message)
        if result is not None:
            _clear_aborted_last_run(run.run_id)
            return True
        return False
    except Exception as e:
        logger.error("Orphan resume attempt failed for run {}: {}", run.run_id, e)
        return False


async def _build_resume_message(run: SubagentRunRecord) -> str | None:
    """Compose a recovery message including chat history and config-change warnings."""
    parts = [
        "[RECOVERY] Your previous execution was interrupted.\n",
        f"Original task: {run.task}\n",
    ]

    chat_history = await _read_chat_history(run.child_session_key)
    if chat_history:
        last_human = chat_history.get("last_human_message")
        last_ai = chat_history.get("last_ai_message")

        if last_human:
            parts.append(f"Last user message:\n{last_human[:500]}\n")
        if last_ai:
            parts.append(f"Last assistant response:\n{last_ai[:500]}\n")

        if chat_history.get("config_changes"):
            parts.append("Note: Configuration changes were detected during the previous session. Please avoid re-applying the same configuration changes.\n")

    elif run.completion.result_text:
        last_output = run.completion.result_text[:500]
        parts.append(f"Last output before interruption:\n{last_output}")

    parts.append("Please continue from where you left off.")
    return "\n".join(parts)


async def _read_chat_history(child_session_key: str) -> dict | None:
    """Extract the last human and AI messages from a session's chat history."""
    try:
        from agent import built_agent
        from pub_func import build_agent_config

        agent = await built_agent()
        state = await agent.aget_state(config=build_agent_config(child_session_key))
        messages = state.values.get("messages", [])

        if not messages:
            return None

        result = {"last_human_message": None, "last_ai_message": None, "config_changes": False}

        for msg in reversed(messages):
            if result["last_human_message"] is None and hasattr(msg, "type") and msg.type == "human":
                content = msg.content if isinstance(msg.content, str) else str(msg.content)
                result["last_human_message"] = content
                if "openclaw.json" in content or "gateway restart" in content:
                    result["config_changes"] = True  # Detect config-change patterns that caused the interruption
            if result["last_ai_message"] is None and hasattr(msg, "type") and msg.type == "ai":
                content = msg.content if isinstance(msg.content, str) else str(msg.content)
                result["last_ai_message"] = content
            if result["last_human_message"] and result["last_ai_message"]:
                break

        return result
    except Exception as e:
        logger.debug("Failed to read chat history for {}: {}", child_session_key, e)
        return None


def _persist_recovery_attempt(run_id: str) -> None:
    """Write the current recovery attempt count back to the run record."""
    run = get_run(run_id)
    if run is None:
        return
    attempts = recovery_attempts_persisted.get(run_id, 0)
    from ..registry.memory import update as update_run
    update_run(run_id, recovery_attempts_persisted=attempts)


def _clear_aborted_last_run(run_id: str) -> None:
    """Clear the aborted_last_run flag after a successful resume."""
    run = get_run(run_id)
    if run is None:
        return
    from ..registry.memory import update as update_run
    update_run(run_id, aborted_last_run=False)


def cancel_recovery(run_id: str) -> None:
    """Cancel a pending recovery task and clear its attempt counter."""
    task = _recovery_tasks.pop(run_id, None)
    if task and not task.done():
        task.cancel()
    recovery_attempts_persisted.pop(run_id, None)


_MAX_TERMINAL_FINALIZE_ATTEMPTS = 3  # Max retries for force-finalizing an interrupted run


async def finalize_interrupted_run_with_retry(
    run_id: str,
    max_attempts: int = _MAX_TERMINAL_FINALIZE_ATTEMPTS,
) -> SubagentRunRecord | None:
    """Force-finalize an interrupted run as TERMINAL/TIMEOUT with exponential backoff retries."""
    for attempt in range(max_attempts):
        run = get_run(run_id)
        if run is None:
            return None
        if run.execution.status == ExecutionStatus.TERMINAL:
            return run

        from ..types.registry import ExecutionState, RunOutcomeStatus
        updated = run.model_copy(update={
            "execution": ExecutionState(
                status=ExecutionStatus.TERMINAL,
                started_at=run.execution.started_at,
                ended_at=time.monotonic(),
                outcome=RunOutcome(status=RunOutcomeStatus.TIMEOUT, error="finalized"),
            ),
            "ended_reason": "finalized",
        })
        set_run(updated)
        try:
            await run_subagent_announce_flow(updated)
            return updated
        except Exception as e:
            logger.error("Finalize interrupted run attempt {} failed for {}: {}", attempt + 1, run_id, e)

        delay = min(1.0 * (2 ** attempt), 8.0)  # Exponential backoff: 1s, 2s, 4s, capped at 8s
        await asyncio.sleep(delay)

    logger.warning("Max terminal finalize attempts reached for run {}", run_id)
    return None


def reclassify_legacy_timeout(run: SubagentRunRecord) -> SubagentRunRecord | None:
    """Reclassify a terminal timeout run with aborted_last_run as INTERRUPTED for recovery."""
    if not run.aborted_last_run:
        return None
    if run.ended_reason != "timeout":
        return None
    if run.execution.status != ExecutionStatus.TERMINAL:
        return None

    updated = run.model_copy(update={
        "ended_reason": "interrupted",
        "execution": run.execution.model_copy(update={
            "status": ExecutionStatus.INTERRUPTED,
            "ended_at": None,
        }),
    })
    set_run(updated)
    logger.info("Reclassified legacy timeout → interrupted for run {}", run.run_id)
    return updated
