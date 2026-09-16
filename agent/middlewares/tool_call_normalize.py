from typing import Any
from langgraph.runtime import Runtime
from loguru import logger
from typing import override
from langchain_core.messages import RemoveMessage, BaseMessage
from pub.func import sanitize_tool_use_result_pairing
from langgraph.graph.message import REMOVE_ALL_MESSAGES
from langchain.agents.middleware import AgentMiddleware, AgentState


class ToolCallNormalize(AgentMiddleware):
    def _before_model_impl(self, state: AgentState) -> dict[str, Any] | None:
        original: list[BaseMessage] = state["messages"]
        normalize_messages: list[BaseMessage] = sanitize_tool_use_result_pairing(original)
        normalize_messages = [m for m in normalize_messages if not isinstance(m, RemoveMessage)]

        # Healthy-transcript fast path: the sanitizer returns the ORIGINAL list
        # object when it repaired nothing, but the RemoveMessage filter still
        # builds a new list — so compare ELEMENT identity, never list identity.
        # Returning None skips a needless state write (and rebuild-induced drift).
        if len(normalize_messages) == len(original) and all(
            a is b for a, b in zip(normalize_messages, original)
        ):
            return None

        return {"messages": [RemoveMessage(id=REMOVE_ALL_MESSAGES), *normalize_messages]}

    @override
    def before_model(self, state: AgentState, runtime: Runtime) -> dict[str, Any] | None:
        logger.debug("{} before_model hook fired", type(self).__name__)
        return self._before_model_impl(state)

    @override
    async def abefore_model(self, state: AgentState, runtime: Runtime) -> dict[str, Any] | None:
        logger.debug("{} abefore_model hook fired", type(self).__name__)
        return self._before_model_impl(state)
