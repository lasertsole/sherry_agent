"""Helper utilities: result text truncation, retry delay calculation, orphan detection, staleness checks, and attachment cleanup."""

import time
from loguru import logger
from ..types.registry import SubagentRunRecord, ExecutionStatus, DeliveryStatus
from ..config import SubagentConfig


def cap_frozen_result_text(text: str | None, max_bytes: int = 24000) -> str | None:
    """Truncate UTF-8 text to max_bytes, appending a truncation notice if exceeded."""
    if text is None:
        return None
    encoded = text.encode("utf-8", errors="replace")
    if len(encoded) <= max_bytes:
        return text
    truncated = encoded[:max_bytes].decode("utf-8", errors="replace")
    return truncated + f"\n... [truncated, {len(encoded)} bytes total]"


def resolve_announce_retry_delay_ms(attempt: int, base_ms: int = 1000) -> int:
    """Compute exponential backoff delay in milliseconds, capped at 8000 ms."""
    capped_ms = 8000
    return min(base_ms * (2 ** attempt), capped_ms)


def resolve_announce_retry_delay_seconds(attempt: int, base_ms: int = 1000) -> float:
    """Compute exponential backoff delay in seconds."""
    return resolve_announce_retry_delay_ms(attempt, base_ms) / 1000.0


def log_announce_give_up(run: SubagentRunRecord) -> None:
    """Log a warning that announce retries have been exhausted for a run."""
    logger.warning(
        "Announce give up for run {}: status={}, attempts={}, last_error={}",
        run.run_id,
        run.delivery.status,
        run.delivery.attempt_count,
        run.delivery.last_error,
    )


def resolve_archive_after_ms(config: SubagentConfig | None = None) -> int:
    """Return the archive-after threshold in milliseconds from config."""
    config = config or SubagentConfig()
    return config.archive_after_minutes * 60 * 1000


def is_live_unended_run(run: SubagentRunRecord) -> bool:
    """Return True if the run is RUNNING or INTERRUPTED (not yet terminated)."""
    return run.execution.status in (ExecutionStatus.RUNNING, ExecutionStatus.INTERRUPTED)


def has_run_ended(run: SubagentRunRecord) -> bool:
    """Return True if the run has reached TERMINAL status."""
    return run.execution.status == ExecutionStatus.TERMINAL


def is_stale_unended_run(run: SubagentRunRecord, threshold_seconds: int | None = None) -> bool:
    """Return True if a live run has exceeded the staleness threshold since it started."""
    if not is_live_unended_run(run):
        return False
    if run.execution.started_at is None:
        return False
    config = SubagentConfig() if threshold_seconds is None else None
    threshold = threshold_seconds or (config.stale_unended_threshold_seconds if config else 7200)
    elapsed = time.monotonic() - run.execution.started_at
    return elapsed > threshold


def should_keep_child_link(run: SubagentRunRecord, recent_window_seconds: int = 1800) -> bool:
    """Decide whether to keep a child session link: live, has active descendants, or recently ended."""
    if is_live_unended_run(run):
        return True

    from .queries import count_active_descendant_runs
    if count_active_descendant_runs(run.child_session_key) > 0:
        return True

    if run.execution.ended_at is not None:
        elapsed = time.monotonic() - run.execution.ended_at
        if elapsed < recent_window_seconds:
            return True

    return False


def reconcile_orphaned_run(run: SubagentRunRecord) -> SubagentRunRecord | None:
    """Mark a stale/orphaned live run as TERMINAL with TIMEOUT outcome; returns updated record or None."""
    if not is_live_unended_run(run):
        return None

    if run.execution.started_at is None:
        return None

    from ..types.registry import RunOutcome, ExecutionState, RunOutcomeStatus

    runtime_ms = run.accumulated_runtime_ms
    if run.execution.started_at and run.execution.status == ExecutionStatus.RUNNING:
        runtime_ms += (time.monotonic() - run.execution.started_at) * 1000

    timeout = 3600.0
    elapsed = time.monotonic() - run.execution.started_at
    if elapsed < timeout:
        if not is_stale_unended_run(run):
            return None

    updated = run.model_copy(update={
        "execution": ExecutionState(
            status=ExecutionStatus.TERMINAL,
            started_at=run.execution.started_at,
            ended_at=time.monotonic(),
            outcome=RunOutcome(status=RunOutcomeStatus.TIMEOUT, error="orphaned"),
        ),
        "accumulated_runtime_ms": runtime_ms,
        "ended_reason": "orphaned",
    })
    logger.info("Reconciled orphaned run {}", run.run_id)
    return updated


def safe_remove_attachments_dir(attachments_dir: str | None, attachments_root_dir: str | None = None) -> None:
    """Safely remove an attachments directory, refusing paths outside the configured root."""
    if attachments_dir is None:
        return
    import shutil
    from pathlib import Path
    p = Path(attachments_dir).resolve()
    if not p.exists():
        return
    if attachments_root_dir:
        root = Path(attachments_root_dir).resolve()
        try:
            p.relative_to(root)
        except ValueError:
            logger.warning(
                "Refusing to remove attachments dir {}: resolved path is not under root {}",
                p, root,
            )
            return
    try:
        shutil.rmtree(p)
    except Exception as e:
        logger.warning("Failed to remove attachments dir {}: {}", attachments_dir, e)
