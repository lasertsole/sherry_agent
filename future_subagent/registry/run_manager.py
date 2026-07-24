"""Core write layer for run records: registration, state transitions, completion, steer replacement, and deletion."""

import time
import uuid
from loguru import logger
from ..types.registry import (
    SubagentRunRecord,
    ExecutionState,
    CompletionState,
    CompletionDeliveryState,
    RunOutcome,
    ExecutionStatus,
    DeliveryStatus,
    KillReconciliationState,
)
from ..types.spawn import SpawnMode, ContextMode
from ..types.capability import SubagentSessionRole, ControlScope
from ..types.lifecycle import outcome_to_ended_reason
from ..config import get_config
from . import memory
from . import store_sqlite


def _resolve_role(depth: int, max_depth: int) -> tuple[SubagentSessionRole, ControlScope]:
    """Determine the session role and control scope based on spawn depth."""
    if depth == 0:
        return SubagentSessionRole.MAIN, ControlScope.CHILDREN
    if depth >= max_depth:
        return SubagentSessionRole.LEAF, ControlScope.NONE
    return SubagentSessionRole.ORCHESTRATOR, ControlScope.CHILDREN


def register_run(
    child_session_key: str,
    requester_session_key: str,
    task: str,
    task_name: str | None = None,
    spawn_mode: SpawnMode = SpawnMode.RUN,
    cleanup: str = "delete",
    context_mode: ContextMode = ContextMode.ISOLATED,
    agent_id: str = "main",
    thinking: str | None = None,
    depth: int = 1,
    label: str | None = None,
    inherited_tool_allow: list[str] | None = None,
    inherited_tool_deny: list[str] | None = None,
    scopes: list[str] | None = None,
    output_schema: dict | None = None,
    attachments_dir: str | None = None,
    attachments_root_dir: str | None = None,
    controller_session_key: str | None = None,
    completion_owner_session_key: str | None = None,
    spawned_by: str | None = None,
    spawned_cwd: str | None = None,
    expects_completion_message: bool = True,
    wake_on_descendant_settle: bool = False,
) -> SubagentRunRecord:
    """Register a new sub-agent run in memory with the given parameters."""
    config = get_config()
    role, control_scope = _resolve_role(depth, config.max_spawn_depth)
    completion_required = spawn_mode == SpawnMode.RUN

    run_id = str(uuid.uuid4())
    from .generation import next_subagent_run_generation
    gen = next_subagent_run_generation(child_session_key)

    run = SubagentRunRecord(
        run_id=run_id,
        task_run_id=run_id,
        child_session_key=child_session_key,
        requester_session_key=requester_session_key,
        task=task,
        task_name=task_name,
        spawn_mode=spawn_mode,
        cleanup=cleanup,
        context_mode=context_mode,
        agent_id=agent_id,
        thinking=thinking,
        depth=depth,
        role=role,
        control_scope=control_scope,
        generation=gen,
        controller_session_key=controller_session_key or requester_session_key,
        completion_owner_session_key=completion_owner_session_key,
        spawned_by=spawned_by,
        spawned_cwd=spawned_cwd,
        expects_completion_message=expects_completion_message,
        wake_on_descendant_settle=wake_on_descendant_settle,
        execution=ExecutionState(
            status=ExecutionStatus.RUNNING,
            started_at=time.monotonic(),
        ),
        completion=CompletionState(required=completion_required),
        delivery=CompletionDeliveryState(
            status=DeliveryStatus.NOT_REQUIRED if not completion_required else DeliveryStatus.PENDING,
        ),
        label=label,
        inherited_tool_allow=inherited_tool_allow or [],
        inherited_tool_deny=inherited_tool_deny or [],
        scopes=scopes or [],
        output_schema=output_schema,
        attachments_dir=attachments_dir,
        attachments_root_dir=attachments_root_dir,
    )

    memory.set_run(run)
    logger.info(
        "Registered subagent run: run_id={}, child={}, requester={}, depth={}, role={}, controller={}",
        run.run_id, child_session_key, requester_session_key, depth, role, run.controller_session_key,
    )
    return run


def mark_run_paused_after_yield(run_id: str) -> SubagentRunRecord | None:
    """Transition a run to INTERRUPTED status after a yield pause, snapshotting accumulated runtime."""
    run = memory.get(run_id)
    if run is None:
        return None

    runtime_ms = _compute_accumulated_runtime_ms(run)
    updated = run.model_copy(update={
        "execution": run.execution.model_copy(update={
            "status": ExecutionStatus.INTERRUPTED,
        }),
        "accumulated_runtime_ms": runtime_ms,
        "pause_reason": "yield",
        "ended_reason": None,
    })
    memory.set_run(updated)
    return updated


def mark_run_running(run_id: str) -> SubagentRunRecord | None:
    """Transition an INTERRUPTED run back to RUNNING, clearing the pause reason."""
    run = memory.get(run_id)
    if run is None:
        return None

    updated = run.model_copy(update={
        "execution": run.execution.model_copy(update={
            "status": ExecutionStatus.RUNNING,
        }),
        "pause_reason": None,
    })
    memory.set_run(updated)
    return updated


def complete_run(
    run_id: str,
    outcome: RunOutcome,
    result_text: str | None = None,
) -> SubagentRunRecord | None:
    """Mark a run as TERMINAL with the given outcome and optional result text.

    If the run is already terminal, this is a no-op.
    """
    run = memory.get(run_id)
    if run is None:
        return None

    if run.execution.status == ExecutionStatus.TERMINAL:
        logger.debug("complete_run: run {} already terminal, skipping", run_id)
        return run

    from .helpers import cap_frozen_result_text

    runtime_ms = _compute_accumulated_runtime_ms(run)
    ended_reason = outcome_to_ended_reason(outcome.status).value
    config = get_config()
    archive_at = time.monotonic() + config.archive_after_minutes * 60

    updated = run.model_copy(update={
        "execution": run.execution.model_copy(update={
            "status": ExecutionStatus.TERMINAL,
            "ended_at": time.monotonic(),
            "outcome": outcome,
        }),
        "completion": run.completion.model_copy(update={
            "result_text": cap_frozen_result_text(result_text),
            "captured_at": time.monotonic(),
        }),
        "accumulated_runtime_ms": runtime_ms,
        "ended_reason": ended_reason,
        "archive_at": archive_at,
    })
    memory.set_run(updated)
    return updated


def replace_run_after_steer(
    run_id: str,
    new_task: str | None = None,
) -> SubagentRunRecord | None:
    run = memory.get(run_id)
    if run is None:
        return None

    runtime_ms = _compute_accumulated_runtime_ms(run)
    updated = run.model_copy(update={
        "generation": run.generation + 1,
        "accumulated_runtime_ms": runtime_ms,
        "task": new_task or run.task,
        "execution": run.execution.model_copy(update={
            "status": ExecutionStatus.INTERRUPTED,
            "outcome": None,
            "ended_at": None,
        }),
        "completion": run.completion.model_copy(update={
            "result_text": None,
            "captured_at": None,
        }),
        "delivery": CompletionDeliveryState(
            status=DeliveryStatus.PENDING if run.completion.required else DeliveryStatus.NOT_REQUIRED,
        ),
        "ended_reason": None,
        "pause_reason": "steer",
        "cleanup_completed_at": None,
        "archive_at": None,
        "ended_hook_emitted": False,
    })
    memory.set_run(updated)
    logger.info(
        "Replaced run after steer: run_id={}, generation={}, accumulated_runtime_ms={:.0f}",
        run_id, updated.generation, updated.accumulated_runtime_ms,
    )
    return updated


def update_run(run_id: str, **kwargs) -> SubagentRunRecord | None:
    """Apply arbitrary field updates to an existing run record."""
    run = memory.get(run_id)
    if run is None:
        return None
    updated = run.model_copy(update=kwargs)
    memory.set_run(updated)
    return updated


async def remove_run(run_id: str) -> SubagentRunRecord | None:
    """Delete a run from both memory and SQLite, returning the removed record."""
    run = memory.delete(run_id)
    if run is not None:
        await store_sqlite.delete_run_from_sqlite(run_id)
    return run


def _compute_accumulated_runtime_ms(run: SubagentRunRecord) -> float:
    """Compute total runtime in ms, adding current elapsed time for RUNNING runs."""
    elapsed = run.accumulated_runtime_ms
    if run.execution.started_at and run.execution.status == ExecutionStatus.RUNNING:
        elapsed += (time.monotonic() - run.execution.started_at) * 1000
    return elapsed


def save_kill_reconciliation(run_id: str) -> SubagentRunRecord | None:
    """Snapshot execution and delivery state for kill reconciliation, marking the run as killed."""
    run = memory.get(run_id)
    if run is None:
        return None
    kr = KillReconciliationState(
        snapshot_execution=run.execution.model_copy(),
        snapshot_delivery=run.delivery.model_copy(),
        killed_at=time.monotonic(),
        reconciled=False,
    )
    updated = run.model_copy(update={"kill_reconciliation": kr})
    memory.set_run(updated)
    return updated


def clear_kill_reconciliation(run_id: str) -> SubagentRunRecord | None:
    """Clear the kill reconciliation snapshot after reconciliation is complete."""
    run = memory.get(run_id)
    if run is None:
        return None
    updated = run.model_copy(update={"kill_reconciliation": None})
    memory.set_run(updated)
    return updated
