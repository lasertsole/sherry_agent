"""Bridge package: expose future_subagent's 7 session-control tools to the main agent.

This package re-wraps ``future_subagent``'s LLM tools (``sessions_spawn``,
``sessions_yield``, ``sessions_send``, ``sessions_kill``, ``sessions_steer``,
``agents_list``, ``subagents_list``) so that ``session_id`` is injected at
**runtime** via LangGraph's ``InjectedState("session_id")`` instead of being
bound at build time.

Design notes
------------
* We deliberately do **not** modify any ``future_subagent`` internals. The
  original tools require ``session_id`` as a constructor field
  (``build_*_tool(session_id=...)``). The main agent, however, registers
  tools through ``_MAIN_TOOLS_BUILDERS`` with zero-argument builders
  (``Callable[[], BaseTool]``) and must look up each session on the fly.
* Every import of ``future_subagent`` internals happens **inside** the
  function bodies (lazy import). This is mandatory to break the circular
  import cycle::

      future_subagent/spawn/core.py
          └─> from agent.tools import build_main_tools   (lazy)
                  └─> agent/tools/__init__.py
                          └─> agent/tools/future_subagent/__init__.py
                                  └─> future_subagent/...  ⚠ cycle at import time

  Deferring the ``future_subagent`` import until invocation keeps module
  import time acyclic.
"""

from typing import Annotated, Literal

from langchain_core.tools import BaseTool, tool
from langgraph.prebuilt.tool_node import InjectedState


SessionId = Annotated[str, InjectedState("session_id")]


def _session_key(session_id: str) -> str:
    """Build the canonical requester session key from a raw session id."""
    return f"agent:main:session:{session_id}"


@tool("sessions_spawn")
async def sessions_spawn_tool(
    task: str,
    session_id: SessionId = "",
    task_name: str | None = None,
    label: str | None = None,
    agent_id: str = "main",
    thinking: str | None = None,
    mode: Literal["run", "session"] = "run",
    cleanup: Literal["delete", "keep"] = "delete",
    context: Literal["isolated", "fork"] = "isolated",
    attachments: list[dict] | None = None,
) -> str:
    """Spawn a subagent to execute a task in the background.

    The subagent runs independently; results are delivered on completion.
    Use for complex or time-consuming tasks that can run on their own.
    """
    # Lazy import to avoid the circular dependency future_subagent <-> agent.tools
    from future_subagent.spawn import spawn_subagent_direct
    from future_subagent.types.spawn import ContextMode, SpawnMode

    requester_session_key = _session_key(session_id)

    spawn_mode = SpawnMode(mode)
    context_mode = ContextMode(context)
    attach_dicts = attachments or None

    result = await spawn_subagent_direct(
        task=task,
        requester_session_key=requester_session_key,
        requester_session_id=session_id,
        agent_id=agent_id,
        task_name=task_name,
        label=label,
        thinking=thinking,
        spawn_mode=spawn_mode,
        cleanup=cleanup,
        context=context_mode,
        attachments=attach_dicts,
        expects_completion_message=True,
    )

    parts = [f"Subagent spawned: status={result.status}"]
    if result.run_id:
        parts.append(f"run_id={result.run_id}")
    if result.child_session_key:
        parts.append(f"session_key={result.child_session_key}")
    if result.task_name:
        parts.append(f"task_name={result.task_name}")
    if result.note:
        parts.append(result.note)
    if result.error:
        parts.append(f"error={result.error}")
    return ", ".join(parts)


@tool("sessions_yield")
async def sessions_yield_tool(
    session_id: SessionId = "",
    reason: str | None = None,
    timeout_seconds: float = 300.0,
) -> str:
    """Pause the current turn and wait for all spawned subagents to complete.

    You are automatically resumed once every subagent result has been
    delivered. Use after spawning subagents when you want their results
    before continuing.
    """
    from future_subagent.registry import (
        get_yield_event,
        register_yield_event,
        remove_yield_event,
    )
    from future_subagent.registry.queries import list_runs_for_requester
    from future_subagent.types.registry import ExecutionStatus

    import asyncio

    session_key = _session_key(session_id)

    children = list_runs_for_requester(session_key)
    active = [
        c for c in children
        if c.execution.status in (ExecutionStatus.RUNNING, ExecutionStatus.INTERRUPTED)
    ]
    if not active:
        return "No active subagents found. You can continue without waiting."

    event = register_yield_event(session_key)
    try:
        await asyncio.wait_for(event.wait(), timeout=timeout_seconds)
        return "All subagents have completed. Their results have been delivered to you."
    except asyncio.TimeoutError:
        return (
            f"Yield timed out after {timeout_seconds}s. Some subagents may still be "
            "running. Use subagents_list to check."
        )
    finally:
        remove_yield_event(session_key)


@tool("sessions_send")
async def sessions_send_tool(
    target_session_key: str,
    message: str,
    session_id: SessionId = "",
    max_turns: int = 1,
) -> str:
    """Send a message to another session (agent-to-agent communication).

    Use to communicate with spawned subagents or other active sessions.
    """
    from future_subagent.control.controller import can_control_run
    from future_subagent.control.send import send_subagent_message
    from future_subagent.registry.queries import get_run_by_child_session_key

    requester_key = _session_key(session_id)

    run = get_run_by_child_session_key(target_session_key)
    if run is None:
        return f"Error: No subagent found with session key '{target_session_key}'"

    allowed, reason = can_control_run(run, requester_key)
    if not allowed:
        return f"Error: Control denied for session '{target_session_key}': {reason}"

    await send_subagent_message(
        run_id=run.run_id,
        message=message,
        caller_session_key=requester_key,
        wait_for_reply=False,
    )
    return f"Message sent to {target_session_key}"


@tool("sessions_kill")
async def sessions_kill_tool(
    run_id: str,
    session_id: SessionId = "",
    cascade: bool = True,
    reason: str = "killed by parent",
) -> str:
    """Kill a running subagent by its run_id.

    Cancels its execution and marks it as killed. Optionally cascade to kill
    all descendant subagents as well.
    """
    from future_subagent.control import kill_subagent_run_with_cascade

    requester_session_key = _session_key(session_id)
    killed = await kill_subagent_run_with_cascade(
        run_id,
        reason=reason,
        cascade=cascade,
        requester_session_key=requester_session_key,
    )
    if not killed:
        return f"No subagent found with run_id={run_id}, or it was already terminated."
    if len(killed) == 1:
        return f"Killed subagent {run_id} (status=killed)."
    ids = [r.run_id for r in killed]
    return f"Killed {len(killed)} subagent(s) (cascade): {', '.join(ids[:8])}{'...' if len(ids) > 8 else ''}"


@tool("sessions_steer")
async def sessions_steer_tool(
    run_id: str,
    new_task: str | None = None,
    new_instructions: str | None = None,
) -> str:
    """Steer a running subagent by injecting new instructions or replacing its task.

    The subagent is interrupted, receives the new direction, and continues
    executing. Provide either ``new_task`` to fully replace the task or
    ``new_instructions`` to add guidance.
    """
    from future_subagent.control import steer_subagent_run

    if not new_task and not new_instructions:
        return "Error: Must provide at least one of new_task or new_instructions."

    result = await steer_subagent_run(
        run_id=run_id,
        new_task=new_task,
        new_instructions=new_instructions,
    )
    if result is None:
        return f"Error: Could not steer subagent {run_id}. It may not exist or is not running."
    return f"Steered subagent {run_id} (generation={result.generation}). It will continue with the new direction."


@tool("agents_list")
async def agents_list_tool() -> str:
    """List available agent IDs that can be used as targets for sessions_spawn."""
    from future_subagent.config import get_config

    config = get_config()
    allow_agents = config.allow_agents
    if "*" in allow_agents:
        return "Available agents: * (all agents allowed). Use agent_id='main' for the default agent."
    agents = [f"- {aid}" for aid in allow_agents]
    return "Available agents:\n" + "\n".join(agents)


@tool("subagents_list")
async def subagents_list_tool(
    session_id: SessionId = "",
) -> str:
    """List active and recent subagent runs for the current session."""
    from future_subagent.control import build_subagent_list

    session_key = _session_key(session_id)
    info = build_subagent_list(session_key)

    lines = [
        f"Subagents: total={info['total']}, active={info['active_count']}, "
        f"recent={info['recent_count']}"
    ]
    if info["active"]:
        lines.append("\nActive:")
        for a in info["active"]:
            lines.append(
                f"  - [{a['run_id'][:8]}] {a['label'] or a['task']} "
                f"(pending_descendants={a['pending_descendants']}, runtime={a['runtime']})"
            )
    if info["recent"]:
        lines.append("\nRecent:")
        for r in info["recent"]:
            lines.append(
                f"  - [{r['run_id'][:8]}] {r['label'] or r['task']} "
                f"(status={r['status']}, runtime={r['runtime']})"
            )
    return "\n".join(lines)


_FUTURE_SUBAGENT_TOOLS: list[BaseTool] = [
    sessions_spawn_tool,
    sessions_yield_tool,
    sessions_send_tool,
    sessions_kill_tool,
    sessions_steer_tool,
    agents_list_tool,
    subagents_list_tool,
]


def build_future_subagent_tools() -> list[BaseTool]:
    """Build and return the 7 future_subagent bridge tools."""
    for t in _FUTURE_SUBAGENT_TOOLS:
        t.handle_tool_error = True
    return list(_FUTURE_SUBAGENT_TOOLS)
