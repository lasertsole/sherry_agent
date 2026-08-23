"""Sub-agent run record and three state-machine models: execution, completion, and delivery."""

from enum import Enum


class ExecutionStatus(str, Enum):
    """Top-level execution phase of a sub-agent run."""
    RUNNING = "running"
    INTERRUPTED = "interrupted"
    TERMINAL = "terminal"


class DeliveryStatus(str, Enum):
    """Lifecycle of a completion-delivery attempt from the sub-agent to its parent."""
    NOT_REQUIRED = "not_required"
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    DELIVERED = "delivered"
    FAILED = "failed"
    SUSPENDED = "suspended"
    DISCARDED = "discarded"


class RunOutcomeStatus(str, Enum):
    """Terminal outcome category for a sub-agent run."""
    OK = "ok"
    ERROR = "error"
    TIMEOUT = "timeout"
    KILLED = "killed"
    UNKNOWN = "unknown"


from pydantic import BaseModel, Field
from typing import Literal


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


class ThreadBindingInfo(BaseModel):
    """Metadata for a SESSION-mode sub-agent bound to a conversation thread."""
    thread_id: str
    bound_at: float = 0.0
    idle_timeout_ms: int = 300000
    max_age_ms: int = 86400000
    delivery_origin: str | None = None


class KillReconciliationState(BaseModel):
    """Snapshot of execution/delivery state taken at kill time, used for graceful reconciliation."""
    snapshot_execution: ExecutionState = Field(default_factory=ExecutionState)
    snapshot_delivery: CompletionDeliveryState = Field(default_factory=CompletionDeliveryState)
    killed_at: float = 0.0
    reconciled: bool = False


from .spawn import SpawnMode, ContextMode
from .capability import SubagentSessionRole, ControlScope


class SubagentRunRecord(BaseModel):
    """Persistent record for a single sub-agent run, encompassing spawn config, lifecycle state, and delivery tracking."""
    run_id: str
    task_run_id: str | None = None
    child_session_key: str
    requester_session_key: str
    task: str
    task_name: str | None = None

    spawn_mode: SpawnMode = SpawnMode.RUN
    cleanup: Literal["delete", "keep"] = "delete"
    context_mode: ContextMode = ContextMode.ISOLATED
    agent_id: str = "main"
    thinking: str | None = None

    depth: int = 1
    role: SubagentSessionRole = SubagentSessionRole.LEAF
    control_scope: ControlScope = ControlScope.NONE

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
    thread_id: str | None = None
    thread_binding_info: ThreadBindingInfo | None = None

    swarm_group_id: str | None = None
    swarm_run_state: str | None = None
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
