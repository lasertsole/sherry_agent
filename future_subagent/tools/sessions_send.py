"""Tool for sending messages to sub-agents with controller ownership checks and ANNOUNCE_SKIP handling."""

from pydantic import BaseModel, Field
from langchain.tools import BaseTool
from loguru import logger

from ..registry import get_run
from ..registry.queries import get_run_by_child_session_key
from ..control.controller import can_control_run


class SessionsSendSchema(BaseModel):
    """Input schema for the sessions_send tool."""
    target_session_key: str = Field(
        description="The session key of the target agent to send a message to."
    )
    message: str = Field(
        description="The message content to send to the target agent."
    )
    max_turns: int = Field(
        default=1,
        description="Maximum ping-pong turns for agent-to-agent communication."
    )


class SessionsSendTool(BaseTool):
    """LLM tool: send a message to a running sub-agent session."""
    name: str = "sessions_send"
    description: str = (
        "Send a message to another session (agent-to-agent communication). "
        "Use to communicate with spawned subagents or other active sessions."
    )
    args_schema: type = SessionsSendSchema

    session_id: str = ""

    def _run(self, **kwargs) -> str:
        raise NotImplementedError("Use async version")

    async def _arun(
        self,
        target_session_key: str,
        message: str,
        max_turns: int = 1,
    ) -> str:
        requester_key = f"agent:main:session:{self.session_id}"

        run = get_run_by_child_session_key(target_session_key)
        if run is None:
            return f"Error: No subagent found with session key '{target_session_key}'"

        allowed, reason = can_control_run(run, requester_key)
        if not allowed:
            return f"Error: Control denied for session '{target_session_key}': {reason}"

        if "ANNOUNCE_SKIP" in message or "⟦ANNOUNCE_SKIP⟧" in message:
            logger.debug("ANNOUNCE_SKIP sentinel detected in send message for run {}", run.run_id)

        from ..control.send import send_subagent_message
        result = await send_subagent_message(
            run_id=run.run_id,
            message=message,
            caller_session_key=requester_key,
            wait_for_reply=False,
        )
        return f"Message sent to {target_session_key}"


def build_sessions_send_tool(session_id: str = "") -> BaseTool:
    """Build and return a sessions_send tool instance."""
    tool = SessionsSendTool(session_id=session_id)
    tool.handle_tool_error = True
    return tool
