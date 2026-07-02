from loguru import logger
from langgraph.runtime import Runtime
from langgraph.typing import ContextT
from typing_extensions import override
from langchain.agents import AgentState
from typing import Any, Callable, Awaitable
from langchain.agents.middleware.types import ResponseT
from workspace.prompt_builder import build_system_prompt
from runtime import state_register_db, state_register_mem
from langchain_core.messages import BaseMessage, SystemMessage, AIMessage, RemoveMessage
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
        state = request.state
        session_id = state.get("session_id", "")
        if session_id.strip() == "":
            err_text: str = "Not pass session_id"
            logger.error(err_text)
            raise RuntimeError(err_text)

        res: dict[str, Any] | None = await super().abefore_model(state, request.runtime)

        # No compression triggered: return directly to preserve prefix cache
        if res is None:
            return await handler(request)
        # Compression triggered: prefix cache is already invalidated, refresh system prompt
        else:
            reduce_messages: list[BaseMessage] = [m for m in res["messages"] if not isinstance(m, RemoveMessage)]

            from tools import memory_store
            memory_store.load_from_disk()

            # Persist the system prompt to both in-memory and DB state stores
            system_prompt: str = build_system_prompt()
            state_register_mem.set_state(session_id, "system_prompt", system_prompt)
            state_register_db.set_state(session_id, "system_prompt", system_prompt)

            return await handler(
                request.override(messages = reduce_messages, system_message = SystemMessage(content = system_prompt))
            )