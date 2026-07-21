from typing import Any
from langgraph.runtime import Runtime
from loguru import logger
from typing_extensions import override
from langchain_core.messages import RemoveMessage, BaseMessage
from pub_func import sanitize_tool_use_result_pairing
from langgraph.graph.message import REMOVE_ALL_MESSAGES
from langchain.agents.middleware import AgentMiddleware, AgentState


class ToolCallNormalize(AgentMiddleware):
    def _before_model_impl(self, state: AgentState) -> dict[str, Any] | None:
        normalize_messages: list[BaseMessage] = sanitize_tool_use_result_pairing(state["messages"])
        normalize_messages = [m for m in normalize_messages if not isinstance(m, RemoveMessage)]

        return {
            "messages": [
                RemoveMessage(id=REMOVE_ALL_MESSAGES),
                *normalize_messages
            ]
        }

    @override
    def before_model(self, state: AgentState, runtime: Runtime) -> dict[str, Any] | None:
        logger.debug("{} before_model hook fired", type(self).__name__)
        return self._before_model_impl(state)

    @override
    async def abefore_model(self, state: AgentState, runtime: Runtime) -> dict[str, Any] | None:
        logger.debug("{} abefore_model hook fired", type(self).__name__)
        return self._before_model_impl(state)