"""Sub-agent run record and three state-machine models: execution, completion, and delivery."""

from __future__ import annotations

import time
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, Field

from .capability import SubagentSessionRole, ControlScope
from .functional_role import FunctionalRole


class ExecutionStatus(StrEnum):
    """Top-level execution phase of a sub-agent run."""

    PENDING = "pending"  # registered, waiting for a SUBAGENT lane slot
    RUNNING = "running"
    INTERRUPTED = "interrupted"
    TERMINAL = "terminal"


class DeliveryStatus(StrEnum):
    """Lifecycle of a completion-delivery attempt from the sub-agent to its parent."""

    NOT_REQUIRED = "not_required"
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    DELIVERED = "delivered"
    FAILED = "failed"
    SUSPENDED = "suspended"
    DISCARDED = "discarded"


class RunOutcomeStatus(StrEnum):
    """Terminal outcome category for a sub-agent run."""

    OK = "ok"
    ERROR = "error"
    TIMEOUT = "timeout"
    KILLED = "killed"
    UNKNOWN = "unknown"


class RunOutcome(BaseModel):
    """Result of a completed sub-agent run, including status and optional error detail."""

    status: RunOutcomeStatus = RunOutcomeStatus.UNKNOWN
    error: str | None = None


class ExecutionState(BaseModel):
    """Tracks whether a sub-agent is running, interrupted, or terminal, with timing and outcome."""

    status: ExecutionStatus = ExecutionStatus.RUNNING
    started_at: float | None = None
    ended_at: float | None = None
    outcome: RunOutcome | None = None
    transcript_target: str | None = None


class CompletionState(BaseModel):
    """Whether a sub-agent result has been captured and is awaiting delivery."""

    required: bool = True
    result_text: str | None = None
    #: The layer's OWN structured handoff (4 experience fields), extracted from
    #: the result text by ``spawn.core._extract_handoff_json`` at completion.
    #: None when the child did not emit the block (the plan extractor then
    #: falls back to the free-text ``result_text``).
    handoff: dict | None = None
    captured_at: float | None = None


class CompletionDeliveryState(BaseModel):
    """Full delivery lifecycle state: attempts, suspension, discard, and final delivery/announce timestamps."""

    status: DeliveryStatus = DeliveryStatus.NOT_REQUIRED
    payload: str | None = None
    attempt_count: int = 0
    last_error: str | None = None
    last_attempt_at: float | None = None
    suspended_at: float | None = None
    discard_reason: str | None = None
    delivered_at: float | None = None
    announced_at: float | None = None


class KillReconciliationState(BaseModel):
    """Snapshot of execution/delivery state taken at kill time, used for graceful reconciliation."""

    snapshot_execution: ExecutionState = Field(default_factory=ExecutionState)
    snapshot_delivery: CompletionDeliveryState = Field(default_factory=CompletionDeliveryState)
    killed_at: float = 0.0
    reconciled: bool = False


class SubagentRunRecord(BaseModel):
    """Persistent record for a single sub-agent run, encompassing spawn config, lifecycle state, and delivery tracking."""

    run_id: str
    task_run_id: str | None = None
    child_session_key: str
    requester_session_key: str
    task: str
    task_name: str | None = None

    cleanup: Literal["delete", "keep"] = "delete"
    agent_id: str = "main"
    thinking: str | None = None
    #: The serving model's configured max-output budget, frozen at spawn. ``None``
    #: = the user left it unset, so the legacy result caps apply.
    max_output_tokens: int | None = None

    depth: int = 1
    role: SubagentSessionRole = SubagentSessionRole.LEAF
    control_scope: ControlScope = ControlScope.NONE
    # Functional specialization (orthogonal to depth role). Defaults to GENERAL,
    # which is the pre-migration behavior: no LLM/tool/prompt override.
    functional_role: FunctionalRole = FunctionalRole.GENERAL

    generation: int = 0

    controller_session_key: str | None = None
    completion_owner_session_key: str | None = None
    expects_completion_message: bool = True
    ended_reason: str | None = None
    pause_reason: str | None = None
    wake_on_descendant_settle: bool = False
    archive_at: float | None = None
    cleanup_completed_at: float | None = None
    accumulated_runtime_ms: float = 0.0
    ended_hook_emitted: bool = False

    kill_reconciliation: KillReconciliationState | None = None
    suppress_announce_reason: str | None = None
    terminal_owner: str | None = None

    aborted_last_run: bool = False
    recovery_attempts_persisted: int = 0

    #: TaskFlow coordinates this run executes (spawn-time context). They let
    #: skill review map a run to its todo DETERMINISTICALLY instead of
    #: guessing from task text; None for a free-form spawn.
    step_id: str | None = None
    flow_id: str | None = None

    swarm_group_id: str | None = None
    swarm_run_state: str | None = None
    launch_fingerprint: str | None = None
    output_schema: dict | None = None
    suppress_completion_delivery: bool = False
    retain_attachments_on_keep: bool = False

    execution: ExecutionState = Field(default_factory=ExecutionState)
    completion: CompletionState = Field(default_factory=CompletionState)
    delivery: CompletionDeliveryState = Field(default_factory=CompletionDeliveryState)

    attachments_dir: str | None = None
    attachments_root_dir: str | None = None

    inherited_tool_allow: list[str] = Field(default_factory=list)
    inherited_tool_deny: list[str] = Field(default_factory=list)
    inherited_tool_policy_version: int = 1
    scopes: list[str] = Field(default_factory=list)
    spawned_by: str | None = None
    spawned_cwd: str | None = None

    label: str | None = None

    # ------------------------------------------------------------------
    # Restart shaping (2026-11-09 audit M17: the record owns its own data)
    # ------------------------------------------------------------------

    def finalize_pending_orphan(self, now: float | None = None) -> SubagentRunRecord:
        """Terminal shape for a restored PENDING run (it never started)."""
        return self.model_copy(
            update={
                "execution": ExecutionState(
                    status=ExecutionStatus.TERMINAL,
                    started_at=None,
                    ended_at=now if now is not None else time.monotonic(),
                    outcome=RunOutcome(status=RunOutcomeStatus.TIMEOUT, error="pending orphaned"),
                ),
                "ended_reason": "pending_orphaned",
            }
        )

    def terminate_on_restart(self, now: float) -> SubagentRunRecord:
        """Terminalize a RUNNING run restored from disk: no task survives a restart."""
        return self.model_copy(
            update={
                "execution": ExecutionState(
                    status=ExecutionStatus.TERMINAL,
                    started_at=self.execution.started_at,
                    ended_at=now,
                    outcome=RunOutcome(
                        status=RunOutcomeStatus.TIMEOUT,
                        error="interrupted by a process restart",
                    ),
                ),
                "pause_reason": "process_restart",
                "ended_reason": "process_restarted",
            }
        )

    def rebase_clock(self, now: float, *, archive_window_seconds: float) -> SubagentRunRecord:
        """Move every persisted monotonic timestamp onto THIS process's clock."""

        def _at(value: float | None) -> float | None:
            return now if value is not None else None

        return self.model_copy(
            update={
                "execution": self.execution.model_copy(
                    update={
                        "started_at": _at(self.execution.started_at),
                        "ended_at": _at(self.execution.ended_at),
                    }
                ),
                "completion": self.completion.model_copy(
                    update={"captured_at": _at(self.completion.captured_at)}
                ),
                "delivery": self.delivery.model_copy(
                    update={
                        "last_attempt_at": _at(self.delivery.last_attempt_at),
                        "suspended_at": _at(self.delivery.suspended_at),
                        "delivered_at": _at(self.delivery.delivered_at),
                        "announced_at": _at(self.delivery.announced_at),
                    }
                ),
                "kill_reconciliation": (
                    self.kill_reconciliation.model_copy(update={"killed_at": now})
                    if self.kill_reconciliation is not None
                    else None
                ),
                # A DEADLINE, not a timestamp: rebasing it to "now" would archive
                # the run on the first sweep. Give it a fresh window instead.
                "archive_at": now + archive_window_seconds if self.archive_at is not None else None,
                "cleanup_completed_at": _at(self.cleanup_completed_at),
            }
        )
