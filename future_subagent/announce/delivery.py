"""Delivery implementation for sub-agent results.

Supports dual-path delivery: sub→sub internal injection vs sub→user completion
message. Classifies errors as transient/permanent and applies appropriate retry
schedules including compaction-retry for nested sub-agent scenarios.
"""

import asyncio
import re
import time
from loguru import logger
from ..types.registry import SubagentRunRecord, DeliveryStatus, RunOutcome
from ..types.delivery import DeliveryContext
from ..config import get_config
from .idempotency import build_idempotency_key
from .dispatch import resolve_dispatch_type, AnnounceDeliveryResult, run_announce_dispatch

_delivered_keys: set[str] = set()  # In-memory idempotency tracking
_delivery_mirror: dict[str, str] = {}  # Content-based deduplication mirror
_MIRROR_MAX = 5000  # Max entries in the delivery mirror before eviction

_TRANSIENT_PATTERNS = [
    re.compile(r"timeout", re.IGNORECASE),
    re.compile(r"connection\s*(reset|refused|aborted)", re.IGNORECASE),
    re.compile(r"temporarily\s*unavailable", re.IGNORECASE),
    re.compile(r"rate\s*limit", re.IGNORECASE),
    re.compile(r"queue\s*full", re.IGNORECASE),
    re.compile(r"retry", re.IGNORECASE),
    re.compile(r"ECONNRESET", re.IGNORECASE),
    re.compile(r"ECONNREFUSED", re.IGNORECASE),
    re.compile(r"ETIMEDOUT", re.IGNORECASE),
]

_PERMANENT_PATTERNS = [
    re.compile(r"not\s*found", re.IGNORECASE),
    re.compile(r"permission\s*denied", re.IGNORECASE),
    re.compile(r"unauthorized", re.IGNORECASE),
    re.compile(r"forbidden", re.IGNORECASE),
    re.compile(r"invalid\s*session", re.IGNORECASE),
    re.compile(r"session\s*expired", re.IGNORECASE),
    re.compile(r"channel\s*not\s*found", re.IGNORECASE),
    re.compile(r"ENOENT", re.IGNORECASE),
]

_TRANSIENT_RETRY_DELAYS_MS = [5000, 10000, 20000]  # Exponential backoff for transient errors
_COMPACTION_RETRY_DELAYS_MS = [1000, 2000, 4000, 8000]  # Backoff for nested sub-agent compaction retries


def _is_already_delivered(run: SubagentRunRecord) -> bool:
    """Check idempotency set to prevent duplicate delivery."""
    key = build_idempotency_key(run.run_id, run.generation)
    return key in _delivered_keys


def _mark_delivered(run: SubagentRunRecord) -> None:
    """Record delivery in the idempotency set, evicting oldest entries when full."""
    key = build_idempotency_key(run.run_id, run.generation)
    _delivered_keys.add(key)
    if len(_delivered_keys) > 10000:  # Evict oldest half when capacity exceeded
        oldest = list(_delivered_keys)[:5000]
        for k in oldest:
            _delivered_keys.discard(k)


def _check_delivery_mirror(run: SubagentRunRecord) -> bool:
    """Content-based deduplication: skip if an identical result was already delivered."""
    content_key = f"{run.child_session_key}:{run.generation}:{(run.completion.result_text or '')[:200]}"  # Truncate to limit key size
    if content_key in _delivery_mirror:
        logger.debug("Delivery mirror hit for run {}, skipping duplicate", run.run_id)
        return True
    _delivery_mirror[content_key] = run.run_id
    if len(_delivery_mirror) > _MIRROR_MAX:  # Evict oldest half when capacity exceeded
        oldest = list(_delivery_mirror.items())[:_MIRROR_MAX // 2]
        for k, _ in oldest:
            _delivery_mirror.pop(k, None)
    return False


def classify_delivery_error(error: str) -> str:
    """Classify a delivery error as 'permanent', 'transient', or 'unknown'."""
    for pattern in _PERMANENT_PATTERNS:
        if pattern.search(error):
            return "permanent"
    for pattern in _TRANSIENT_PATTERNS:
        if pattern.search(error):
            return "transient"
    return "unknown"


def resolve_transient_retry_delay_ms(attempt: int) -> float:
    """Return the retry delay in seconds for the given transient error attempt index."""
    if attempt < len(_TRANSIENT_RETRY_DELAYS_MS):
        return _TRANSIENT_RETRY_DELAYS_MS[attempt] / 1000.0
    return _TRANSIENT_RETRY_DELAYS_MS[-1] / 1000.0


def resolve_compaction_retry_delay_ms(attempt: int) -> float:
    """Return the retry delay in seconds for the given compaction attempt index."""
    if attempt < len(_COMPACTION_RETRY_DELAYS_MS):
        return _COMPACTION_RETRY_DELAYS_MS[attempt] / 1000.0
    return _COMPACTION_RETRY_DELAYS_MS[-1] / 1000.0


async def deliver_subagent_announcement(run: SubagentRunRecord) -> AnnounceDeliveryResult:
    """Deliver a sub-agent's completion announcement with idempotency, suspension, and retry logic."""
    config = get_config()

    if _is_already_delivered(run):
        return AnnounceDeliveryResult(success=True)

    if _check_delivery_mirror(run):
        return AnnounceDeliveryResult(success=True)

    if not run.completion.required:
        from ..registry import mark_delivery_delivered, set_run
        updated = mark_delivery_delivered(run)
        set_run(updated)
        return AnnounceDeliveryResult(success=True)

    from ..registry import (
        mark_delivery_in_progress,
        mark_delivery_delivered,
        mark_delivery_failed,
        mark_delivery_suspended,
        set_run,
        count_pending_descendant_runs,
    )

    pending_count = count_pending_descendant_runs(run.requester_session_key)
    if pending_count >= config.delivery_suspend_hard_cap:
        updated = mark_delivery_suspended(run)
        set_run(updated)
        return AnnounceDeliveryResult(success=False, suspended=True, error="Hard cap exceeded")

    updated = mark_delivery_in_progress(run)
    set_run(updated)
    run = updated

    target_key = run.requester_session_key
    try:
        from ..hooks.progress import fire_delivery_target_hook
        redirect = await fire_delivery_target_hook(run, target_key)
        if redirect is not None:
            logger.info("Delivery target hook redirected run {} from {} to {}", run.run_id, target_key, redirect)
            run = run.model_copy(update={"requester_session_key": redirect})
            set_run(run)
    except Exception as e:
        logger.debug("fire_delivery_target_hook error: {}", e)

    result = await run_announce_dispatch(run, _deliver_with_retry)

    if result.success:
        _mark_delivered(run)
        updated = mark_delivery_delivered(run)
        set_run(updated)
    elif not result.suspended:
        now = time.monotonic()
        from ..registry.delivery_state import should_discard_delivery
        if should_discard_delivery(run, config.max_announce_retry_count, config.announce_hard_expiry_ms, now):
            from ..registry import mark_delivery_discarded
            updated = mark_delivery_discarded(run, reason="max_retries_or_expiry")
            set_run(updated)
            result.terminal = True
        elif pending_count >= config.delivery_suspend_soft_cap:
            updated = mark_delivery_suspended(run)
            set_run(updated)
            result.suspended = True

    return result


async def _deliver_with_retry(run: SubagentRunRecord, **kwargs) -> AnnounceDeliveryResult:
    """Attempt delivery with retries, classifying errors and applying appropriate backoff."""
    config = get_config()
    compaction_attempt = 0

    for attempt in range(config.announce_retry_max):
        try:
            ctx = _build_delivery_context(run)
            if ctx.is_requester_subagent:
                await _deliver_internal_injection(ctx)
            else:
                await _deliver_completion_message(ctx)
            return AnnounceDeliveryResult(success=True)
        except Exception as e:
            error_str = str(e)
            error_class = classify_delivery_error(error_str)
            logger.warning(
                "Announce delivery attempt {}/{} failed for run {} [{}]: {}",
                attempt + 1, config.announce_retry_max, run.run_id, error_class, error_str,
            )

            if error_class == "permanent":
                logger.error("Permanent delivery error for run {}, aborting: {}", run.run_id, error_str)
                return AnnounceDeliveryResult(success=False, error=f"permanent: {error_str}")

            from ..registry import mark_delivery_failed, set_run
            updated = mark_delivery_failed(run, error_str)
            set_run(updated)
            run = updated

            if error_class == "transient":
                delay = resolve_transient_retry_delay_ms(attempt)
            elif error_class == "unknown" and ":subagent:" in (run.requester_session_key or ""):
                # Nested sub-agent requester may need compaction retry instead of standard backoff
                compaction_attempt += 1
                delay = resolve_compaction_retry_delay_ms(compaction_attempt - 1)
            else:
                from ..registry.helpers import resolve_announce_retry_delay_seconds
                delay = resolve_announce_retry_delay_seconds(attempt, config.announce_retry_delay_base_ms)

            await asyncio.sleep(delay)

    return AnnounceDeliveryResult(success=False, error="Max retries exceeded")


def _build_delivery_context(run: SubagentRunRecord) -> DeliveryContext:
    """Build a DeliveryContext from a run record, resolving the announce origin."""
    from .origin import resolve_announce_origin
    origin = resolve_announce_origin(run)

    return DeliveryContext(
        requester_session_key=run.requester_session_key,
        child_session_key=run.child_session_key,
        child_label=run.label,
        task=run.task,
        result_text=run.completion.result_text,
        outcome=run.execution.outcome or RunOutcome(),
        run_id=run.run_id,
        depth=run.depth,
        is_requester_subagent=origin.is_requester_subagent,
    )


async def _deliver_internal_injection(ctx: DeliveryContext) -> None:
    """Deliver result as an internal injection to a sub-agent requester session."""
    from bus import MessageBus
    from type.bus import InboundMessage

    status = "ok" if ctx.outcome.status == "ok" else ctx.outcome.status.value
    content = f"[Subagent Internal] {ctx.child_label or ctx.task[:30]}: {status}"
    if ctx.result_text:
        summary = ctx.result_text[:500]
        content += f"\n{summary}"

    msg = InboundMessage(
        channel="system",
        sender_id="subagent_internal",
        chat_id="direct",
        content=content,
        session_id=ctx.requester_session_key,
        metadata={
            "injected_event": "subagent_internal_update",
            "subagent_run_id": ctx.run_id,
            "subagent_child_session_key": ctx.child_session_key,
            "internal": True,
        },
    )

    bus = MessageBus()
    await bus.publish_inbound(msg)


async def _deliver_completion_message(ctx: DeliveryContext) -> None:
    """Deliver result as a user-facing completion message to the requester session."""
    from bus import MessageBus
    from type.bus import InboundMessage

    status_text = "completed successfully"
    if ctx.outcome.status == "killed":
        status_text = "killed"
    elif ctx.outcome.status == "error":
        status_text = f"failed: {ctx.outcome.error or 'unknown error'}"
    elif ctx.outcome.status == "timeout":
        status_text = "timed out"

    content_parts = [f"**[Subagent Task]** [{ctx.child_label or ctx.task[:30]}]"]
    content_parts.append(f"Status: {status_text}")
    content_parts.append(f"Task: {ctx.task}")
    if ctx.result_text:
        text = ctx.result_text
        if len(text) > 4000:  # Truncate long results to prevent oversized messages
            text = text[:4000] + "\n... [truncated]"
        content_parts.append(f"Result:\n{text}")
    content_parts.append("\nPlease review the sub-agent execution results above. Provide further instructions if needed.")

    msg = InboundMessage(
        channel="system",
        sender_id="subagent",
        chat_id="direct",
        content="\n\n".join(content_parts),
        session_id=ctx.requester_session_key,
        metadata={
            "injected_event": "subagent_result",
            "subagent_run_id": ctx.run_id,
            "subagent_child_session_key": ctx.child_session_key,
        },
    )

    bus = MessageBus()
    await bus.publish_inbound(msg)
