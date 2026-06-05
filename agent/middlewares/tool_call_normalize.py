from typing import Any

from langgraph.runtime import Runtime
from langchain_core.messages import RemoveMessage
from pub_func import sanitize_tool_use_result_pairing
from langgraph.graph.message import REMOVE_ALL_MESSAGES
from langchain.agents.middleware import AgentMiddleware, AgentState


class ToolCallNormalize(AgentMiddleware):
    def __init__(self, session_id: str):
        super().__init__()
        self._session_id: str = session_id

    async def abefore_agent(self, state: AgentState, runtime: Runtime) -> dict[str, Any] | None:
        return {
            "messages": [
                RemoveMessage(id=REMOVE_ALL_MESSAGES),
                *sanitize_tool_use_result_pairing(state["messages"])
            ]
        }

    async def aafter_agent(self, state: AgentState, runtime: Runtime) -> dict[str, Any] | None:
        return {
            "messages": [
                RemoveMessage(id=REMOVE_ALL_MESSAGES),
                *sanitize_tool_use_result_pairing(state["messages"])
            ]
        }