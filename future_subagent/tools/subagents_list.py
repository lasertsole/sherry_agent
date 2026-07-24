"""Tool for listing active and recent sub-agent runs for the current session."""

from pydantic import BaseModel, Field
from langchain.tools import BaseTool
from typing import Any
from ..control import build_subagent_list


class SubagentsListSchema(BaseModel):
    """Input schema for the subagents_list tool (no parameters)."""
    pass


class SubagentsListTool(BaseTool):
    """LLM tool: list active and recent sub-agent runs for the current session."""
    name: str = "subagents_list"
    description: str = (
        "List active and recent subagent runs for the current session."
    )
    args_schema: type = SubagentsListSchema

    session_id: str = ""

    def _run(self, **kwargs) -> str:
        raise NotImplementedError("Use async version")

    async def _arun(self) -> str:
        # Build session key from current session_id
        session_key = f"agent:main:session:{self.session_id}"
        info = build_subagent_list(session_key)

        lines = [f"Subagents: total={info['total']}, active={info['active_count']}, recent={info['recent_count']}"]

        # Format active sub-agent info
        if info["active"]:
            lines.append("\nActive:")
            for a in info["active"]:
                lines.append(f"  - [{a['run_id'][:8]}] {a.get('label', a['task'])} (depth={a['depth']}, role={a['role']})")

        # Format recently completed sub-agent info
        if info["recent"]:
            lines.append("\nRecent:")
            for r in info["recent"]:
                lines.append(f"  - [{r['run_id'][:8]}] {r.get('label', r['task'])} status={r['status']}")

        return "\n".join(lines)


def build_subagents_list_tool(session_id: str = "") -> BaseTool:
    """Build and return a subagents_list tool instance."""
    tool = SubagentsListTool(session_id=session_id)
    tool.handle_tool_error = True
    return tool
