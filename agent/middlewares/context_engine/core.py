from loguru import logger
from langgraph.typing import ContextT
from typing import override
from typing import Any
from collections.abc import Callable, Awaitable
from workspace.prompt_builder import build_system_prompt
from runtime import state_register_db, state_register_mem
from langchain.agents.middleware import AgentMiddleware, ModelResponse, ModelRequest
from langchain_core.messages import AIMessage, SystemMessage
from langchain.agents.middleware.types import ResponseT, ExtendedModelResponse
from agent.middlewares.base import require_session_id


class ContextEngineHook(AgentMiddleware):
    """Injects the session system prompt into every model call.

    Turn finalization — persisting messages to MesMemory and scheduling the
    memory-review / plan-extraction nudges — is owned by the compression
    pipeline (``Summarization`` + ``context_engine.nudge``), which flushes the
    original discarded prefix before replacing it.
    """

    def __init__(self):
        super().__init__()

    @staticmethod
    def _get_and_reload_system_prompt(session_id) -> str:
        system_prompt = state_register_mem.get_state(session_id, "system_prompt", None)

        if system_prompt is None:
            system_prompt = state_register_db.get_state(session_id, "system_prompt", None)

            if system_prompt is None:
                system_prompt = build_system_prompt(session_id=session_id)
                state_register_db.set_state(session_id, "system_prompt", system_prompt)

            state_register_mem.set_state(session_id, "system_prompt", system_prompt)

        return system_prompt

    # ------------------------------------------------------------------
    # Shared: session validation
    # ------------------------------------------------------------------
    @staticmethod
    def _get_session_id_or_raise(state: Any) -> str:
        return require_session_id(state, "Not pass session_id")

    # ------------------------------------------------------------------
    # Shared: system prompt injection (called by both sync and async)
    # ------------------------------------------------------------------
    def _wrap_model_call_impl(
        self,
        request: ModelRequest[ContextT],
    ) -> ModelRequest[ContextT]:
        """Inject system prompt into the request.

        Returns the (possibly overridden) request. When the request already
        carries a SystemMessage with identical content, the request is returned
        untouched: no new SystemMessage object and no override are created, so
        the model-visible prefix stays byte-identical.
        """
        session_id = self._get_session_id_or_raise(request.state)
        prompt_str = self._get_and_reload_system_prompt(session_id)
        existing = request.system_message
        if isinstance(existing, SystemMessage) and existing.content == prompt_str:
            return request
        return request.override(system_message=SystemMessage(content=prompt_str))

    @override
    def wrap_model_call(
        self,
        request: ModelRequest[ContextT],
        handler: Callable[[ModelRequest[ContextT]], ModelResponse[ResponseT]],
    ) -> ModelResponse[ResponseT] | AIMessage | ExtendedModelResponse[ResponseT]:
        logger.debug("{} wrap_model_call hook fired", type(self).__name__)
        request = self._wrap_model_call_impl(request)
        return handler(request)

    @override
    async def awrap_model_call(
        self,
        request: ModelRequest[ContextT],
        handler: Callable[[ModelRequest[ContextT]], Awaitable[ModelResponse[ResponseT]]],
    ) -> ModelResponse[ResponseT] | AIMessage | ExtendedModelResponse[ResponseT]:
        logger.debug("{} awrap_model_call hook fired", type(self).__name__)
        request = self._wrap_model_call_impl(request)
        return await handler(request)
