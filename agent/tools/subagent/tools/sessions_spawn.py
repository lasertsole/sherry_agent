"""Tool for spawning sub-agents, invoked by the LLM to delegate tasks."""

from pydantic import BaseModel, Field
from langchain.tools import BaseTool

from ..spawn import spawn_subagent_direct
from ..spawn.privilege import check_spawn_permission
from ..types.spawn import SpawnMode


class AttachmentSchema(BaseModel):
    """Schema for a single file attachment passed to a sub-agent."""

    name: str = Field(description="File name for the attachment.")
    content: str = Field(description="File content (text or base64 encoded).")
    encoding: str = Field(default="utf8", description="Content encoding: 'utf8' or 'base64'.")
    mount_path: str | None = Field(
        default=None, description="Optional subdirectory within the attachment area."
    )


class SessionsSpawnSchema(BaseModel):
    task: str = Field(
        description="The task description for the subagent to execute. Be detailed and specific."
    )
    task_name: str | None = Field(
        default=None, description="Optional stable alias for targeting this subagent later."
    )
    label: str | None = Field(
        default=None, description="Optional short display label for the subagent."
    )
    agent_id: str = Field(
        default="main", description="Target agent ID to spawn. Defaults to 'main'."
    )
    thinking: str | None = Field(default=None, description="Optional thinking level override.")
    mode: str = Field(
        default="run",
        description="Spawn mode: 'run' (ephemeral one-shot) or 'session' (persistent).",
    )
    cleanup: str = Field(
        default="delete",
        description="Cleanup policy: 'delete' (remove session after completion) or 'keep'.",
    )
    attachments: list[AttachmentSchema] | None = Field(
        default=None,
        description="Optional list of file attachments to pass to the subagent. "
        "Each attachment has name, content, encoding (utf8/base64), and optional mount_path.",
    )
    goal_max_turns: int | None = Field(
        default=None,
        description="Optional goal-loop turn budget override (including the first "
        "turn); defaults to the configured completion-judge budget.",
    )
    functional_role: str | None = Field(
        default=None,
        description="Functional specialization of the subagent worker. "
        "general=full access, researcher=read-only search, "
        "executor=write+run, reviewer=read-only audit, "
        "librarian=read-only external codebase retrieval. "
        "Omit to inherit the default role.",
    )
    extra_tools: list[str] | None = Field(
        default=None,
        description="Additional tool names to attach for this spawn only "
        "(deepagents per-task tool pattern).",
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
        attachments: list[AttachmentSchema] | None = None,
        goal_max_turns: int | None = None,
        functional_role: str | None = None,
        extra_tools: list[str] | None = None,
    ) -> str:
        # Convert string parameters to enum types
        spawn_mode = SpawnMode(mode)

        # Call-time privilege gate: a non-spawning caller (LEAF) must not spawn even
        # if a tool instance leaked into its toolset. Returns through the tool's
        # existing string contract instead of raising.
        allowed, reason = check_spawn_permission(self.session_id)
        if not allowed:
            return f"Subagent spawn denied: status=forbidden, error={reason}"

        # Build the requester session key from the current session_id
        requester_session_key = f"agent:main:session:{self.session_id}"

        # Flatten AttachmentSchema list to dict list for internal APIs
        attach_dicts = None
        if attachments:
            attach_dicts = [
                {
                    "name": a.name,
                    "content": a.content,
                    "encoding": a.encoding,
                    "mount_path": a.mount_path,
                }
                for a in attachments
            ]

        result = await spawn_subagent_direct(
            task=task,
            requester_session_key=requester_session_key,
            agent_id=agent_id,
            task_name=task_name,
            label=label,
            thinking=thinking,
            spawn_mode=spawn_mode,
            cleanup=cleanup,
            attachments=attach_dicts,
            expects_completion_message=True,
            goal_max_turns=goal_max_turns,
            functional_role_hint=functional_role,
            extra_tools=extra_tools,
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
