"""Multi-level future_subagent system for concurrent task execution and result delivery.

Core pipeline:
- Spawn: validate → register → build child agent → execute as background asyncio.Task
- Announce: on completion → capture result → deliver to parent session via MessageBus
- Registry: in-memory + SQLite persisted run records, three state machines

Usage:
    from future_subagent import build_sessions_spawn_tool, init_registry
"""

from .types import (
    SpawnMode,
    ContextMode,
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


def build_sessions_spawn_tool(session_id: str = ""):
    """Build the sessions_spawn tool for LLM to spawn child agent tasks."""
    from .tools.sessions_spawn import build_sessions_spawn_tool as _build
    return _build(session_id)

def build_sessions_yield_tool():
    """Build the sessions_yield tool for the parent agent to wait for children."""
    from .tools.sessions_yield import build_sessions_yield_tool as _build
    return _build()

def build_sessions_send_tool(session_id: str = ""):
    """Build the sessions_send tool for bidirectional agent messaging."""
    from .tools.sessions_send import build_sessions_send_tool as _build
    return _build(session_id)

def build_agents_list_tool():
    """Build the agents_list tool returning the allow_agents whitelist."""
    from .tools.agents_list import build_agents_list_tool as _build
    return _build()

def build_subagents_list_tool(session_id: str = ""):
    """Build the subagents_list tool showing active and recent child agents."""
    from .tools.subagents_list import build_subagents_list_tool as _build
    return _build(session_id)

def build_sessions_kill_tool(session_id: str = ""):
    """Build the sessions_kill tool to cancel a running child agent."""
    from .tools.sessions_kill import build_sessions_kill_tool as _build
    return _build(session_id)

def build_sessions_steer_tool(session_id: str = ""):
    """Build the sessions_steer tool to inject new instructions into a running child."""
    from .tools.sessions_steer import build_sessions_steer_tool as _build
    return _build(session_id)

__all__ = [
    "SpawnMode",
    "ContextMode",
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
    "build_sessions_spawn_tool",
    "build_sessions_yield_tool",
    "build_sessions_send_tool",
    "build_agents_list_tool",
    "build_subagents_list_tool",
    "build_sessions_kill_tool",
    "build_sessions_steer_tool",
]
