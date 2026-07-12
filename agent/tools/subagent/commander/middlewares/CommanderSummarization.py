from loguru import logger
from langgraph.typing import ContextT
from typing_extensions import override
from typing import Any, Awaitable, Callable, cast
from langchain.agents import AgentState
from agent.tools.xp_graph import update_draft
from langchain.agents.middleware.types import ResponseT
from langchain_core.messages import AnyMessage, BaseMessage, AIMessage, RemoveMessage
from langchain.agents.middleware import SummarizationMiddleware, ModelRequest, ModelResponse, ExtendedModelResponse


class CommanderSummarization(SummarizationMiddleware):
    """Summarization middleware for commander (pass-through for now)."""

    # ------------------------------------------------------------------
    # Shared session validation
    # ------------------------------------------------------------------
    @staticmethod
    def _get_session_or_raise(state: AgentState) -> str:
        session_id: str = state.get("session_id", "")
        if session_id.strip() == "":
            err_text: str = "Not pass session_id"
            logger.error(err_text)
            raise RuntimeError(err_text)
        return session_id

    @override
    def wrap_model_call(
        self,
        request: ModelRequest[ContextT],
        handler: Callable[[ModelRequest[ContextT]], ModelResponse[ResponseT]],
    ) -> ModelResponse[ResponseT] | AIMessage | ExtendedModelResponse[ResponseT]:
        session_id: str = self._get_session_or_raise(request.state)
        system_prompt: str = request.system_prompt or ""
        messages: list[AnyMessage] = request.messages

        update_draft(session_id, system_prompt, cast("list[Any]", messages))

        res: dict[str, Any] | None = super().before_model(request.state, cast("Any", request.runtime))
        if res is None:
            return handler(request)

        reduce_messages: list[BaseMessage] = [m for m in res["messages"] if not isinstance(m, RemoveMessage)]
        override_kwargs: dict[str, Any] = {
            "messages": cast("list[AnyMessage]", reduce_messages),
        }

        return handler(request.override(**override_kwargs))

    @override
    async def awrap_model_call(
        self,
        request: ModelRequest[ContextT],
        handler: Callable[[ModelRequest[ContextT]], Awaitable[ModelResponse[ResponseT]]],
    ) -> ModelResponse[ResponseT] | AIMessage | ExtendedModelResponse[ResponseT]:
        session_id: str = self._get_session_or_raise(request.state)
        system_prompt: str = request.system_prompt or ""
        messages: list[AnyMessage] = request.messages

        update_draft(session_id, system_prompt, cast("list[Any]", messages))

        res: dict[str, Any] | None = await super().abefore_model(request.state, cast("Any", request.runtime))
        if res is None:
            return await handler(request)

        reduce_messages: list[BaseMessage] = [m for m in res["messages"] if not isinstance(m, RemoveMessage)]
        override_kwargs: dict[str, Any] = {
            "messages": cast("list[AnyMessage]", reduce_messages),
        }

        return await handler(request.override(**override_kwargs))

    @override
    def before_model(self, state: AgentState[Any], runtime) -> dict[str, Any] | None:
        return None

    @override
    async def abefore_model(self, state: AgentState[Any], runtime) -> dict[str, Any] | None:
        return None
