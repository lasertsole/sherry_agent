"""Spawn tool for creating background subagents."""

import time
import asyncio
from loguru import logger
from typing import Any, Type
from langchain.tools import BaseTool
from .base import SubagentManager, subagent_manager
from pydantic import BaseModel, Field, validate_call

class SubagentInput(BaseModel):
    task: str = Field(..., description="Subtasks to execute")

class SubagentTool(BaseTool):
    """Tool to spawn a subagent for background task execution."""
    name: str = "subagent"
    description: str = "Spawn a subagent to handle a task in the background. Use for complex or time-consuming tasks that can run independently."
    args_schema: Type[BaseModel] = SubagentInput

    def __init__(self, session_id: str | None = None,  **kwargs: Any):
        super().__init__(**kwargs)
        self._session_id: str | None = session_id
        self._manager: SubagentManager = subagent_manager

    @validate_call(validate_return = True)
    async def _arun(self, task: str, label: str | None = None, **kwargs: Any):
        start_time = time.time()
        task_preview = task[:100] if task else ""
        logger.info(
            f"Subagent tool called: session_id={self._session_id}, "
            f"label={label}, task_preview='{task_preview}'"
        )
        
        if self._session_id is None:
            raise ValueError("Session ID is required")

        """Spawn a subagent to execute the given task."""
        event_loop = self._manager.get_event_loop()

        if not event_loop.is_running():
            logger.error("Subagent event loop is not running")
            raise RuntimeError(
                "Subagent event loop is not running. "
                "Please ensure subagent service has been started."
            )

        try:
            future = asyncio.run_coroutine_threadsafe(
                self._manager.spawn(
                    task=task,
                    label=label,
                    session_id=self._session_id,
                ),
                event_loop
            )

            result = await asyncio.wrap_future(future)
            elapsed = time.time() - start_time
            logger.info(
                f"Subagent tool completed: session_id={self._session_id}, "
                f"label={label}, duration={elapsed:.2f}s"
            )
            return result
        except Exception as e:
            elapsed = time.time() - start_time
            logger.error(
                f"Subagent tool failed: session_id={self._session_id}, "
                f"label={label}, duration={elapsed:.2f}s, error={str(e)}"
            )
            raise

    @validate_call
    def _run(self, task: str, label: str | None = None, **kwargs: Any) -> str:
        """Spawn a subagent to execute the given task."""
        raise RuntimeError(
            "SubagentTool 必须使用异步接口 (_arun)。"
            "同步调用会导致死锁风险。"
        )

def build_subagent_tool(session_id: str | None = None) -> SubagentTool:
    tool: SubagentTool = SubagentTool(session_id)
    tool.handle_tool_error = True
    return tool