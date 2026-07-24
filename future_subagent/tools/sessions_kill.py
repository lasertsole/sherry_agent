"""Tool for killing sub-agents, with optional cascade to descendant runs."""

from pydantic import BaseModel, Field
from langchain.tools import BaseTool
from loguru import logger

from ..control import kill_subagent_run_with_cascade


class SessionsKillSchema(BaseModel):
    """Input schema for the sessions_kill tool."""
    run_id: str = Field(
        description="The run_id of the subagent to kill."
    )
    cascade: bool = Field(
        default=True,
        description="If true, also kill all descendant subagents of the target. Default true."
    )
    reason: str = Field(
        default="killed by parent",
        description="Reason for killing the subagent."
    )


class SessionsKillTool(BaseTool):
    """LLM tool: kill a running sub-agent, cancelling its execution and marking it as KILLED."""
    name: str = "sessions_kill"
    description: str = (
        "Kill a running subagent by its run_id. "
        "Cancels its execution and marks it as killed. "
        "Optionally cascade to kill all its descendant subagents."
    )
    args_schema: type = SessionsKillSchema

    session_id: str = ""

    def _run(self, **kwargs) -> str:
        raise NotImplementedError("Use async version")

    async def _arun(
        self,
        run_id: str,
        cascade: bool = True,
        reason: str = "killed by parent",
    ) -> str:
        logger.info("sessions_kill called: run_id={}, cascade={}, reason={}", run_id, cascade, reason)

        requester_session_key = f"agent:main:session:{self.session_id}"

        killed = await kill_subagent_run_with_cascade(
            run_id, reason=reason, cascade=cascade,
            requester_session_key=requester_session_key,
        )

        if not killed:
            return f"No subagent found with run_id={run_id}, or it was already terminated."

        if len(killed) == 1:
            return f"Killed subagent {run_id} (status=killed)."

        ids = [r.run_id for r in killed]
        return f"Killed {len(killed)} subagent(s) (cascade): {', '.join(ids[:8])}{'...' if len(ids) > 8 else ''}"


def build_sessions_kill_tool(session_id: str = "") -> BaseTool:
    """Build and return a sessions_kill tool instance."""
    tool = SessionsKillTool(session_id=session_id)
    tool.handle_tool_error = True
    return tool
