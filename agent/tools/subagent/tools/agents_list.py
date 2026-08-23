"""Tool to list available agent IDs for sessions_spawn targeting."""

from pydantic import BaseModel, Field
from langchain.tools import BaseTool
from ..config import get_config


class AgentsListSchema(BaseModel):
    """Input schema for the agents_list tool (no parameters)."""
    pass


class AgentsListTool(BaseTool):
    """List agent IDs that can be used as targets for sessions_spawn."""
    name: str = "agents_list"
    description: str = (
        "List available agent IDs that can be used as targets for sessions_spawn."
    )
    args_schema: type = AgentsListSchema

    def _run(self, **kwargs) -> str:
        raise NotImplementedError("Use async version")

    async def _arun(self) -> str:
        config = get_config()
        allow_agents = config.allow_agents

        # Wildcard means all agents are available
        if "*" in allow_agents:
            return "Available agents: * (all agents allowed). Use agent_id='main' for the default agent."

        agents = [f"- {aid}" for aid in allow_agents]
        return "Available agents:\n" + "\n".join(agents)


def build_agents_list_tool() -> BaseTool:
    """Build and return an agents_list tool instance."""
    tool = AgentsListTool()
    tool.handle_tool_error = True
    return tool
