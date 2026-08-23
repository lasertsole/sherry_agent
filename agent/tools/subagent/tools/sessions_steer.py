"""Tool for steering a running sub-agent by injecting new instructions or replacing its task."""

from pydantic import BaseModel, Field
from langchain.tools import BaseTool
from loguru import logger

from ..control import steer_subagent_run


class SessionsSteerSchema(BaseModel):
    """Input schema for the sessions_steer tool."""
    run_id: str = Field(
        description="The run_id of the subagent to steer."
    )
    new_task: str | None = Field(
        default=None,
        description="Optional new task description to replace the original task entirely."
    )
    new_instructions: str | None = Field(
        default=None,
        description="Additional instructions to inject into the running subagent. "
                    "Use this to redirect or refine the subagent's approach without replacing the whole task."
    )


class SessionsSteerTool(BaseTool):
    """LLM tool: steer a running sub-agent by injecting new instructions or replacing its task."""
    name: str = "sessions_steer"
    description: str = (
        "Steer a running subagent by injecting new instructions or replacing its task. "
        "The subagent will be interrupted, receive the new direction, and continue executing. "
        "Provide either new_task to fully replace the task, or new_instructions to add guidance."
    )
    args_schema: type = SessionsSteerSchema

    session_id: str = ""

    def _run(self, **kwargs) -> str:
        raise NotImplementedError("Use async version")

    async def _arun(
        self,
        run_id: str,
        new_task: str | None = None,
        new_instructions: str | None = None,
    ) -> str:
        logger.info("sessions_steer called: run_id={}, new_task={}, new_instructions={}",
                     run_id, bool(new_task), bool(new_instructions))

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


def build_sessions_steer_tool(session_id: str = "") -> BaseTool:
    """Build and return a sessions_steer tool instance."""
    tool = SessionsSteerTool(session_id=session_id)
    tool.handle_tool_error = True
    return tool
