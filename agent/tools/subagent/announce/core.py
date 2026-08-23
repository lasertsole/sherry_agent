"""Core entry point for the sub-agent announce pipeline.

Handles suppress-announce checks, requester-type-aware routing (sub→sub internal
injection vs sub→user completion message), SILENT_REPLY_TOKEN processing, and
descendant-wake deferral.
"""

import time
from loguru import logger
from ..types.registry import SubagentRunRecord, ExecutionStatus, CompletionState
from .output import build_child_completion_findings
from .delivery import deliver_subagent_announcement
from .capture import capture_subagent_completion_reply
from .idempotency import build_idempotency_key
from .origin import resolve_announce_origin
from ..registry import (
    is_delivery_delivered,
    is_delivery_suspended,
    set_run,
    get_run,
)
from ..registry.memory import update as update_run
from ..registry.helpers import safe_remove_attachments_dir
from ..registry.completion import emit_ended_hook_once
from ..config import get_config

SILENT_REPLY_TOKEN = "⟦ANNOUNCE_SKIP⟧"  # Sentinel that suppresses announce delivery when present in result text


async def run_subagent_announce_flow(run: SubagentRunRecord) -> None:
    """Execute the full announce flow for a completed sub-agent run.

    Checks preconditions (terminal, required, not already delivered, not suppressed),
    captures the completion reply if missing, resolves the announce origin, defers
    if descendant runs are still active, then delivers the announcement.
    """
    if run.execution.status != ExecutionStatus.TERMINAL:
        logger.debug("Skipping announce for non-terminal run {}", run.run_id)
        return

    if not run.completion.required:
        logger.debug("Skipping announce for non-required completion run {}", run.run_id)
        return

    if is_delivery_delivered(run):
        logger.debug("Skipping already delivered announce for run {}", run.run_id)
        return

    if run.suppress_announce_reason:
        logger.debug(
            "Skipping announce for run {}: suppress_reason={}",
            run.run_id, run.suppress_announce_reason,
        )
        return

    if _is_silent_reply(run):
        logger.debug("Skipping announce for run {}: silent reply detected", run.run_id)
        from ..registry import mark_delivery_delivered
        updated = mark_delivery_delivered(run)
        set_run(updated)
        await emit_ended_hook_once(updated)
        return

    if not run.completion.result_text or not run.completion.result_text.strip():
        # Result text may not be populated yet; poll with retries
        captured = await capture_subagent_completion_reply(
            child_session_key=run.child_session_key,
            wait_for_reply=True,
            max_wait_ms=5000,
            retry_interval_ms=500,
        )
        if captured and captured.strip():
            # Update the run record with the newly captured result text
            updated = update_run(
                run.run_id,
                completion=CompletionState(
                    required=run.completion.required,
                    result_text=captured,
                    captured_at=time.monotonic(),
                ),
            )
            if updated:
                run = updated

    origin = resolve_announce_origin(run)

    from ..registry.queries import count_active_descendant_runs
    pending_descendants = count_active_descendant_runs(run.child_session_key)
    if pending_descendants > 0 and run.wake_on_descendant_settle:
        # Defer announce until all descendant runs settle
        logger.info(
            "Deferring announce for run {}: {} descendant(s) still active",
            run.run_id, pending_descendants,
        )
        from ..registry.settle_wake import get_settle_wake_batch
        batch = get_settle_wake_batch()
        batch.register_run_for_settle(run.run_id, run.requester_session_key)
        batch.schedule_settle_wake_retry(run.requester_session_key, delay=5.0)
        await emit_ended_hook_once(run)
        return

    findings = build_child_completion_findings(run)
    if findings:
        logger.info("Child completion findings for run {}: {}", run.run_id, findings[:200])

    try:
        result = await deliver_subagent_announcement(run)
        if result.success:
            logger.info("Announce delivered for run {} via {}", run.run_id, "→".join(result.dispatch_path) or "direct")
        elif result.suspended:
            logger.warning("Announce suspended for run {}", run.run_id)
        elif result.terminal:
            logger.error("Announce terminal failure for run {}: {}", run.run_id, result.error)
        else:
            logger.error("Announce failed for run {}: {}", run.run_id, result.error)
    except Exception as e:
        logger.error("Announce flow exception for run {}: {}", run.run_id, e)

    await emit_ended_hook_once(run)

    if run.cleanup == "delete":
        try:
            safe_remove_attachments_dir(run.attachments_dir)
        except Exception as e:
            logger.debug("Cleanup skipped for run {}: {}", run.run_id, e)

    try:
        from ..registry import wake_yield_if_all_children_settled
        woke = await wake_yield_if_all_children_settled(run.requester_session_key)
        if woke:
            logger.info("Woke yield-paused parent after run {} settled", run.run_id)
    except Exception as e:
        logger.debug("Yield wake check failed for run {}: {}", run.run_id, e)

    _schedule_descendant_wake_if_needed(run)


def _is_silent_reply(run: SubagentRunRecord) -> bool:
    """Check whether the run's result text contains the silent-reply sentinel."""
    text = run.completion.result_text
    if text and SILENT_REPLY_TOKEN in text:
        return True
    if text and "ANNOUNCE_SKIP" in text:
        return True
    return False


def _schedule_descendant_wake_if_needed(run: SubagentRunRecord) -> None:
    """Schedule a delayed check to wake the parent once all descendants settle."""
    if not run.wake_on_descendant_settle:
        return

    import asyncio

    async def _check():
        await asyncio.sleep(5.0)
        from ..registry.queries import count_active_descendant_runs
        active = count_active_descendant_runs(run.child_session_key)
        if active == 0:
            try:
                from ..registry import wake_yield_if_all_children_settled
                await wake_yield_if_all_children_settled(run.requester_session_key)
            except Exception:
                pass

    asyncio.create_task(_check())
