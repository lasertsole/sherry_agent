"""Multi-level subagent system for concurrent task execution and result delivery.

Core pipeline:
- Spawn: validate → register → build child agent → execute as background asyncio.Task
- Announce: on completion → capture result → deliver to parent session via EventBus
- Registry: in-memory + SQLite persisted run records, three state machines

Usage:
    from agent.tools.subagent import init_registry
    from agent.tools.subagent.tools.sessions_spawn import build_sessions_spawn_tool
"""

from .types import (
    ExecutionStatus,
    DeliveryStatus,
    RunOutcomeStatus,
    RunOutcome,
    ExecutionState,
    CompletionState,
    CompletionDeliveryState,
    SubagentRunRecord,
    LifecycleEndedReason,
    LifecycleEndedOutcome,
    DeliveryContext,
    SubagentSessionRole,
    ControlScope,
)
from .config import SubagentConfig, get_config, set_config
from .registry import init_registry, persist_runs_to_disk
from .spawn import spawn_subagent_direct, SpawnResult
from .announce import run_subagent_announce_flow
from .delegate import DelegatedTaskHandle, delegate_task


__all__ = [
    "ExecutionStatus",
    "DeliveryStatus",
    "RunOutcomeStatus",
    "RunOutcome",
    "ExecutionState",
    "CompletionState",
    "CompletionDeliveryState",
    "SubagentRunRecord",
    "LifecycleEndedReason",
    "LifecycleEndedOutcome",
    "DeliveryContext",
    "SubagentSessionRole",
    "ControlScope",
    "SubagentConfig",
    "get_config",
    "set_config",
    "init_registry",
    "persist_runs_to_disk",
    "spawn_subagent_direct",
    "SpawnResult",
    "run_subagent_announce_flow",
    "delegate_task",
    "DelegatedTaskHandle",
]
