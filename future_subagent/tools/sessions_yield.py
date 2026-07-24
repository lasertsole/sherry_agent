"""Tool for yielding the current turn, pausing until all child sub-agents complete."""

import asyncio
from pydantic import BaseModel, Field
from langchain.tools import BaseTool
from loguru import logger

from ..registry import register_yield_event, get_yield_event, remove_yield_event
from ..registry.queries import list_runs_for_requester
from ..types.registry import ExecutionStatus


class SessionsYieldSchema(BaseModel):
    reason: str | None = Field(
        default=None,
        description="Optional reason for yielding (e.g., 'waiting for subagents')."
    )
    timeout_seconds: float = Field(
        default=300.0,
        description="Maximum seconds to wait for subagent results. Default 300 (5 min)."
    )


class SessionsYieldTool(BaseTool):
    """LLM tool: pause the current turn and auto-resume when all spawned sub-agents complete."""
    name: str = "sessions_yield"
    description: str = (
        "Pause your current turn and wait for all spawned subagents to complete. "
        "You will be automatically resumed when all subagent results have been delivered. "
        "Use after spawning subagents when you want to receive their results before continuing."
    )
    args_schema: type = SessionsYieldSchema

    session_id: str = ""

    def _run(self, **kwargs) -> str:
        raise NotImplementedError("Use async version")

    async def _arun(self, reason: str | None = None, timeout_seconds: float = 300.0) -> str:
        session_key = f"agent:main:session:{self.session_id}"
        logger.info("sessions_yield called: session={}, reason={}", session_key, reason)

        # Check for active child sub-agents
        children = list_runs_for_requester(session_key)
        active = [
            c for c in children
            if c.execution.status in (ExecutionStatus.RUNNING, ExecutionStatus.INTERRUPTED)
        ]

        if not active:
            return "No active subagents found. You can continue without waiting."

        # Register a yield event; the parent will be woken by announce/core.py wake_yield
        event = register_yield_event(session_key)

        try:
            # Wait for the event to be set (triggered by announce/core.py wake_yield)
            await asyncio.wait_for(event.wait(), timeout=timeout_seconds)
            return "All subagents have completed. Their results have been delivered to you."
        except asyncio.TimeoutError:
            logger.warning("sessions_yield timed out after {}s for session {}", timeout_seconds, session_key)
            return f"Yield timed out after {timeout_seconds}s. Some subagents may still be running. Use subagents_list to check."
        finally:
            remove_yield_event(session_key)


def build_sessions_yield_tool(session_id: str = "") -> BaseTool:
    """Build and return a sessions_yield tool instance."""
    tool = SessionsYieldTool(session_id=session_id)
    tool.handle_tool_error = True
    return tool
