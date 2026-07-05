import asyncio
from asyncio import Task
from loguru import logger
from langgraph.types import Command
from langgraph.runtime import Runtime
from langgraph.typing import ContextT
from typing_extensions import override
from context_engine import add_messages
from typing import Callable, Awaitable, Any, Sequence, cast
from workspace.prompt_builder import build_system_prompt
from langgraph.prebuilt.tool_node import ToolCallRequest
from runtime import state_register_db, state_register_mem
from .nudge import _nudge_memory, _nudge_skill, _nudge_combined
from pub_func.message.slice_last_turn import slice_last_turn
from pub_func.transcript_repair import sanitize_tool_use_result_pairing
from langchain.agents.middleware import AgentMiddleware, ModelResponse, ModelRequest
from langchain_core.messages import BaseMessage, AIMessage, ToolMessage, SystemMessage
from langchain.agents.middleware.types import ResponseT, ExtendedModelResponse, StateT


# Nudge config keys
_NUDGE_MEMORY_COUNT_KEY = "nudge_review_memory_count"
_NUDGE_SKILL_COUNT_KEY = "nudge_review_skill_count"
_NUDGE_MEMORY_LOCK_KEY = "nudge_review_memory_lock"
_NUDGE_SKILL_LOCK_KEY = "nudge_review_skill_lock"
_NUDGE_MEMORY_THRESHOLD = 10
_NUDGE_SKILL_THRESHOLD = 10


class ContextEngineHook(AgentMiddleware):
    def __init__(self):
        super().__init__()

    @staticmethod
    def _is_lock(session_id: str)-> bool:
        return state_register_mem.get_state(session_id, _NUDGE_MEMORY_LOCK_KEY, False) or state_register_mem.get_state(session_id, _NUDGE_SKILL_LOCK_KEY, False)

    @staticmethod
    def _get_and_reload_system_prompt(session_id)-> str:
        system_prompt = state_register_mem.get_state(session_id, "system_prompt", None)

        if system_prompt is None:
            system_prompt = state_register_db.get_state(session_id, "system_prompt", None)

            if system_prompt is None:
                system_prompt = build_system_prompt()
                state_register_db.set_state(session_id, "system_prompt", system_prompt)

            state_register_mem.set_state(session_id, "system_prompt", system_prompt)

        return system_prompt

    # ------------------------------------------------------------------
    # Shared: session validation
    # ------------------------------------------------------------------
    @staticmethod
    def _get_session_id_or_raise(state: Any) -> str:
        session_id: str = state.get("session_id", "")
        if session_id.strip() == "":
            err_text: str = "Not pass session_id"
            logger.error(err_text)
            raise RuntimeError(err_text)
        return session_id

    # ------------------------------------------------------------------
    # Shared: system prompt injection (called by both sync and async)
    # ------------------------------------------------------------------
    def _wrap_model_call_impl(
        self,
        request: ModelRequest[ContextT],
    ) -> ModelRequest[ContextT]:
        """Inject system prompt into the request.

        Returns the (possibly overridden) request.
        """
        return request.override(
            system_message=SystemMessage(
                content=self._get_and_reload_system_prompt(
                    self._get_session_id_or_raise(request.state)
                )
            )
        )

    # ------------------------------------------------------------------
    # Shared: format & persist last turn messages
    # ------------------------------------------------------------------
    def _persist_last_turn(self, state: Any, session_id: str) -> None:
        all_messages: list[BaseMessage] = cast("list[BaseMessage]", state["messages"])
        last_turn_messages: list[BaseMessage] = slice_last_turn(all_messages)["messages"]
        format_last_turn_messages: list[BaseMessage] = sanitize_tool_use_result_pairing(last_turn_messages)
        # Sync log — actual async persistence is handled in the async hook
        logger.debug("_persist_last_turn: session={}, messages={}", session_id, len(format_last_turn_messages))

    # ------------------------------------------------------------------
    # Shared: tool call nudge counter (called by both sync and async)
    # ------------------------------------------------------------------
    def _wrap_tool_call_impl(
        self,
        request: ToolCallRequest,
    ) -> None:
        """Increment nudge skill counter."""
        session_id = self._get_session_id_or_raise(request.state)
        nudge_review_skill_count: int = state_register_db.get_state(session_id, _NUDGE_SKILL_COUNT_KEY, 0) + 1
        state_register_db.set_state(session_id, _NUDGE_SKILL_COUNT_KEY, nudge_review_skill_count)

    @override
    def wrap_model_call(
        self,
        request: ModelRequest[ContextT],
        handler: Callable[[ModelRequest[ContextT]], ModelResponse[ResponseT]],
    ) -> ModelResponse[ResponseT] | AIMessage | ExtendedModelResponse[ResponseT]:
        request = self._wrap_model_call_impl(request)
        res: ModelResponse = handler(request)

        session_id = self._get_session_id_or_raise(request.state)
        self._persist_last_turn(request.state, session_id)

        return res

    @override
    async def awrap_model_call(
        self,
        request: ModelRequest[ContextT],
        handler: Callable[[ModelRequest[ContextT]], Awaitable[ModelResponse[ResponseT]]],
    ) -> ModelResponse[ResponseT] | AIMessage | ExtendedModelResponse[ResponseT]:
        request = self._wrap_model_call_impl(request)
        res: ModelResponse = await handler(request)

        session_id = self._get_session_id_or_raise(request.state)

        # Persist user messages to MesMemory (async)
        all_messages: list[BaseMessage] = cast("list[BaseMessage]", request.state["messages"])
        last_turn_messages: list[BaseMessage] = slice_last_turn(all_messages)["messages"]
        format_last_turn_messages: list[BaseMessage] = sanitize_tool_use_result_pairing(last_turn_messages)
        add_history_task: Task[None] = asyncio.create_task(add_messages(session_id=session_id, messages=format_last_turn_messages))
        await asyncio.gather(add_history_task)

        return res

    @override
    def wrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], ToolMessage | Command[Any]],
    ) -> ToolMessage | Command[Any]:
        self._wrap_tool_call_impl(request)
        return handler(request)

    @override
    async def awrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], Awaitable[ToolMessage | Command[Any]]],
    ) -> ToolMessage | Command[Any]:
        self._wrap_tool_call_impl(request)
        return await handler(request)

    # ------------------------------------------------------------------
    # Shared: nudge logic (session validation + count management)
    # ------------------------------------------------------------------
    def _after_agent_impl(
        self, state: StateT
    ) -> tuple[str, str, list[BaseMessage], bool, bool] | None:
        """Validate session, increment counters, and decide if nudge should fire.

        Returns (session_id, system_prompt, messages, need_memory, need_skill)
        or None if the agent should bail early (lock active).
        """
        session_id: str = state.get("session_id", "")
        if session_id.strip() == "":
            err_text: str = "Not pass session_id"
            logger.error(err_text)
            raise RuntimeError(err_text)

        messages: list[BaseMessage] = cast("list[BaseMessage]", state["messages"])
        system_prompt: str = self._get_and_reload_system_prompt(session_id)

        nudge_review_memory_count: int = (
            state_register_db.get_state(session_id, _NUDGE_MEMORY_COUNT_KEY, 0) + 1
        )
        nudge_review_skill_count: int = state_register_db.get_state(
            session_id, _NUDGE_SKILL_COUNT_KEY, 0
        )

        # If nudge is locked, skip this turn
        if self._is_lock(session_id):
            state_register_db.set_state(
                session_id, _NUDGE_MEMORY_COUNT_KEY, nudge_review_memory_count
            )
            return None

        need_nudge_review_memory: bool = (
            nudge_review_memory_count >= _NUDGE_MEMORY_THRESHOLD
        )
        need_nudge_skill_memory: bool = (
            nudge_review_skill_count >= _NUDGE_SKILL_THRESHOLD
        )

        logger.info(
            "nudge_review_memory_count is {}, need_nudge_review_memory is {}",
            nudge_review_memory_count,
            need_nudge_review_memory,
        )
        logger.info(
            "nudge_review_skill_count is {}, need_nudge_skill_memory is {}",
            nudge_review_skill_count,
            need_nudge_skill_memory,
        )

        if need_nudge_skill_memory and need_nudge_review_memory:
            state_register_db.set_state(session_id, _NUDGE_MEMORY_COUNT_KEY, 0)
            state_register_db.set_state(session_id, _NUDGE_SKILL_COUNT_KEY, 0)
        else:
            if need_nudge_review_memory:
                state_register_db.set_state(session_id, _NUDGE_MEMORY_COUNT_KEY, 0)
            else:
                state_register_db.set_state(
                    session_id, _NUDGE_MEMORY_COUNT_KEY, nudge_review_memory_count
                )

            if need_nudge_skill_memory:
                state_register_db.set_state(session_id, _NUDGE_SKILL_COUNT_KEY, 0)

        return session_id, system_prompt, messages, need_nudge_review_memory, need_nudge_skill_memory

    @override
    def after_agent(
        self, state: StateT, runtime: Runtime[ContextT]
    ) -> dict[str, Any] | None:
        result = self._after_agent_impl(state)
        if result is None:
            return None

        session_id, system_prompt, messages, need_memory, need_skill = result

        if need_memory and need_skill:
            asyncio.get_event_loop().run_until_complete(
                _nudge_combined(session_id, system_prompt, messages)
            )
        else:
            if need_memory:
                asyncio.get_event_loop().run_until_complete(
                    _nudge_memory(session_id, system_prompt, messages)
                )
            if need_skill:
                asyncio.get_event_loop().run_until_complete(
                    _nudge_skill(session_id, system_prompt, messages)
                )

        return None

    @override
    async def aafter_agent(
        self, state: StateT, runtime: Runtime[ContextT]
    ) -> dict[str, Any] | None:
        result = self._after_agent_impl(state)
        if result is None:
            return None

        session_id, system_prompt, messages, need_memory, need_skill = result

        if need_memory and need_skill:
            await _nudge_combined(session_id, system_prompt, messages)
        else:
            if need_memory:
                await _nudge_memory(session_id, system_prompt, messages)
            if need_skill:
                await _nudge_skill(session_id, system_prompt, messages)

        return None