from loguru import logger
from langgraph.runtime import Runtime
from langgraph.typing import ContextT
from typing_extensions import override
from langchain.agents import AgentState
from context_engine import nudge_messages
from typing import Any, Callable, Awaitable
from langchain.agents.middleware.types import ResponseT
from workspace.prompt_builder import build_system_prompt
from langchain_core.messages import BaseMessage, SystemMessage, AIMessage
from langchain.agents.middleware import SummarizationMiddleware, ModelRequest, ModelResponse, ExtendedModelResponse


class Summarization(SummarizationMiddleware):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)

    @override
    async def abefore_model(
        self, state: AgentState[Any], runtime: Runtime[ContextT]
    ) -> dict[str, Any] | None:
        pass

    @override
    async def awrap_model_call(
        self,
        request: ModelRequest[ContextT],
        handler: Callable[[ModelRequest[ContextT]], Awaitable[ModelResponse[ResponseT]]],
    ) -> ModelResponse[ResponseT] | AIMessage | ExtendedModelResponse[ResponseT]:
        session_id = request.state.get("session_id", "")
        if session_id.strip() == "":
            err_text: str = "Not pass session_id"
            logger.error(err_text)
            raise RuntimeError(err_text)

        state: dict[str, Any] = request.state
        state_mes_list_copy_without_system_mes: list[BaseMessage] = [m for m in state["messages"].copy() if not isinstance(m, SystemMessage)]

        copy_state: AgentState[Any] = state.copy()
        copy_state["messages"] = state_mes_list_copy_without_system_mes

        res: dict[str, Any] | None = await super().abefore_model(copy_state, request.runtime)
        if res is None:
            if request.system_message is None:
                return await handler(request.override(system_message = SystemMessage(content = build_system_prompt())))

            return await handler(request)

        reduce_messages: list[BaseMessage] = res["messages"]

        from tools import memory_store
        memory_store.load_from_disk()

        await nudge_messages(session_id=session_id, nudge_turn=0)

        memory_store.load_from_disk()

        return await handler(
            request.override(messages = reduce_messages, system_message = SystemMessage(content = build_system_prompt()))
        )