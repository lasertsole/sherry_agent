"""Multi-level subagent system for concurrent task execution and result delivery.

Core pipeline:
- Spawn: validate → register → build child agent → execute as background asyncio.Task
- Announce: on completion → capture result → deliver to parent session via EventBus
- Registry: in-memory + SQLite persisted run records, three state machines

Usage:
    from agent.tools.subagent import build_sessions_spawn_tool, init_registry
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
from .delegate import DelegatedTaskHandle, delegate_task


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


def build_subagent_runtime_tools():
    """Build the 7 runtime-InjectedState subagent tools for the main agent.

    ``session_id`` is injected per-invocation from the current LangGraph state,
    so a single zero-arg builder serves the main agent and all recursively
    spawned subagents.
    """
    from .tools.runtime_tools import build_subagent_runtime_tools as _build

    return _build()


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
    "delegate_task",
    "DelegatedTaskHandle",
    "build_sessions_spawn_tool",
    "build_sessions_yield_tool",
    "build_sessions_send_tool",
    "build_agents_list_tool",
    "build_subagents_list_tool",
    "build_sessions_kill_tool",
    "build_sessions_steer_tool",
    "build_subagent_runtime_tools",
]
