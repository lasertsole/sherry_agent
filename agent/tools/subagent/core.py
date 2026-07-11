"""Spawn tool for creating background subagents."""

import time
import asyncio
from loguru import logger
from typing import Annotated
from .base import subagent_manager
from pydantic import BaseModel, Field
from langchain.tools import BaseTool, tool
from langgraph.prebuilt.tool_node import InjectedState

class SubagentSchema(BaseModel):
    """Schema for the subagent tool."""
    task: str = Field(
        description="The task description for the subagent to execute. Be detailed and specific about what needs to be done."
    )
    label: str | None = Field(
        default=None,
        description="Optional short label/name for the subagent task, used for display in notifications."
    )


@tool("subagent", args_schema=SubagentSchema)
async def subagent_tool(
    task: str,
    label: str | None = None,
    session_id: Annotated[str, InjectedState("session_id")] = "",
) -> str:
    """Spawn a subagent to handle a task in the background. Use for complex or time-consuming tasks that can run independently."""

    start_time = time.time()
    task_preview = task[:100] if task else ""
    logger.info(
        f"Subagent tool called: session_id={session_id}, "
        f"label={label}, task_preview='{task_preview}'"
    )

    if not session_id:
        raise ValueError("Session ID is required")

    event_loop = subagent_manager.get_event_loop()

    if not event_loop.is_running():
        logger.error("Subagent event loop is not running")
        raise RuntimeError(
            "Subagent event loop is not running. "
            "Please ensure subagent service has been started."
        )

    try:
        future = asyncio.run_coroutine_threadsafe(
            subagent_manager.spawn(
                task=task,
                label=label,
                session_id=session_id,
            ),
            event_loop
        )

        result = await asyncio.wrap_future(future)
        return result
    except Exception as e:
        elapsed = time.time() - start_time
        logger.error(
            f"Subagent tool failed: session_id={session_id}, "
            f"label={label}, duration={elapsed:.2f}s, error={str(e)}"
        )
        raise


def build_subagent_tool() -> BaseTool:
    """Return the subagent tool with error handling enabled."""
    subagent_tool.handle_tool_error = True
    return subagent_tool