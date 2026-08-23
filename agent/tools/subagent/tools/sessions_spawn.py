"""Tool for spawning sub-agents, invoked by the LLM to delegate tasks."""

from pydantic import BaseModel, Field
from langchain.tools import BaseTool
from typing import Annotated, Any
from langgraph.prebuilt.tool_node import InjectedState

from ..spawn import spawn_subagent_direct
from ..types.spawn import SpawnMode, ContextMode


class AttachmentSchema(BaseModel):
    """Schema for a single file attachment passed to a sub-agent."""
    name: str = Field(description="File name for the attachment.")
    content: str = Field(description="File content (text or base64 encoded).")
    encoding: str = Field(default="utf8", description="Content encoding: 'utf8' or 'base64'.")
    mount_path: str | None = Field(default=None, description="Optional subdirectory within the attachment area.")


class SessionsSpawnSchema(BaseModel):
    task: str = Field(
        description="The task description for the subagent to execute. Be detailed and specific."
    )
    task_name: str | None = Field(
        default=None,
        description="Optional stable alias for targeting this subagent later."
    )
    label: str | None = Field(
        default=None,
        description="Optional short display label for the subagent."
    )
    agent_id: str = Field(
        default="main",
        description="Target agent ID to spawn. Defaults to 'main'."
    )
    thinking: str | None = Field(
        default=None,
        description="Optional thinking level override."
    )
    mode: str = Field(
        default="run",
        description="Spawn mode: 'run' (ephemeral one-shot) or 'session' (persistent)."
    )
    cleanup: str = Field(
        default="delete",
        description="Cleanup policy: 'delete' (remove session after completion) or 'keep'."
    )
    context: str = Field(
        default="isolated",
        description="Context mode: 'isolated' (clean slate) or 'fork' (inherit parent transcript)."
    )
    attachments: list[AttachmentSchema] | None = Field(
        default=None,
        description="Optional list of file attachments to pass to the subagent. "
                    "Each attachment has name, content, encoding (utf8/base64), and optional mount_path."
    )


class SessionsSpawnTool(BaseTool):
    """LLM tool: spawn a sub-agent to execute a task; results are auto-delivered on completion."""
    name: str = "sessions_spawn"
    description: str = (
        "Spawn a subagent to execute a task. "
        "The subagent runs in the background and results will be delivered when it completes. "
        "Use for complex or time-consuming tasks that can run independently."
    )
    args_schema: type = SessionsSpawnSchema

    session_id: str = ""

    def _run(self, **kwargs) -> str:
        raise NotImplementedError("Use async version")

    async def _arun(
        self,
        task: str,
        task_name: str | None = None,
        label: str | None = None,
        agent_id: str = "main",
        thinking: str | None = None,
        mode: str = "run",
        cleanup: str = "delete",
        context: str = "isolated",
        attachments: list[AttachmentSchema] | None = None,
    ) -> str:
        # Convert string parameters to enum types
        spawn_mode = SpawnMode(mode)
        context_mode = ContextMode(context)

        # Build the requester session key from the current session_id
        requester_session_key = f"agent:main:session:{self.session_id}"

        # Flatten AttachmentSchema list to dict list for internal APIs
        attach_dicts = None
        if attachments:
            attach_dicts = [
                {"name": a.name, "content": a.content, "encoding": a.encoding, "mount_path": a.mount_path}
                for a in attachments
            ]

        result = await spawn_subagent_direct(
            task=task,
            requester_session_key=requester_session_key,
            requester_session_id=self.session_id,
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


def build_sessions_spawn_tool(session_id: str = "") -> BaseTool:
    """Build and return a sessions_spawn tool instance."""
    tool = SessionsSpawnTool(session_id=session_id)
    tool.handle_tool_error = True
    return tool
