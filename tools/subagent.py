"""Spawn tool for creating background subagents."""

import asyncio
from typing import Any, Type
from langchain.tools import BaseTool
from pydantic import BaseModel, Field
from subagent import SubagentManager, subagent_manager

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

    async def _arun(self, task: str, label: str | None = None, **kwargs: Any):
        if self._session_id is None:
            raise ValueError("Session ID is required")

        """Spawn a subagent to execute the given task."""
        event_loop = self._manager.get_event_loop()

        if not event_loop.is_running():
            raise RuntimeError(
                "Subagent event loop is not running. "
                "Please ensure subagent service has been started."
            )

        future = asyncio.run_coroutine_threadsafe(
            self._manager.spawn(
                task=task,
                label=label,
                session_id=self._session_id,
            ),
            event_loop
        )

        return await asyncio.wrap_future(future)

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