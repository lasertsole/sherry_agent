import asyncio
from asyncio import Task
from loguru import logger
from langgraph.types import Command
from langgraph.runtime import Runtime
from langgraph.typing import ContextT
from typing_extensions import override
from context_engine import add_messages
from typing import Callable, Awaitable, Any
from workspace.prompt_builder import build_system_prompt
from langgraph.prebuilt.tool_node import ToolCallRequest
from runtime import state_register_db, state_register_mem
from .nudge import _nudge_memory, _nudge_skill, _nudge_combined
from pub_func import slice_last_turn, sanitize_tool_use_result_pairing
from langchain.agents.middleware import AgentMiddleware, ModelResponse, ModelRequest
from langchain_core.messages import BaseMessage, AIMessage, ToolMessage, SystemMessage
from langchain.agents.middleware.types import ResponseT, ExtendedModelResponse, StateT


class ContextEngineHook(AgentMiddleware):
    def __init__(self):
        super().__init__()

    @staticmethod
    def _is_lock(session_id: str)-> bool:
        return state_register_mem.get_state(session_id, "nudge_review_memory_lock", False) or state_register_mem.get_state(session_id, "nudge_review_skill_lock", False)

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

    @override
    async def awrap_model_call(
        self,
        request: ModelRequest[ContextT],
        handler: Callable[[ModelRequest[ContextT]], Awaitable[ModelResponse[ResponseT]]],
    ) -> ModelResponse[ResponseT] | AIMessage | ExtendedModelResponse[ResponseT]:
        state = request.state
        session_id: str = state.get("session_id", "")
        if session_id.strip() == "":
            err_text: str = "Not pass session_id"
            logger.error(err_text)
            raise RuntimeError(err_text)

        # using system prompt through cache which match session_id
        request = request.override(system_message = SystemMessage(content=self._get_and_reload_system_prompt(session_id)))

        res: ModelResponse = await handler(request)

        # Get the formatted message list of the last conversation turn
        all_messages: list[BaseMessage] = state["messages"]
        last_turn_messages: list[BaseMessage] = slice_last_turn(all_messages)["messages"]
        format_last_turn_messages: list[BaseMessage] = sanitize_tool_use_result_pairing(last_turn_messages)

        # Persist user messages to MesMemory
        add_history_task: Task = asyncio.create_task(add_messages(session_id = session_id, messages=format_last_turn_messages))

        await asyncio.gather(add_history_task)

        return res

    @override
    async def awrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], Awaitable[ToolMessage | Command[Any]]],
    ) -> ToolMessage | Command[Any]:
        session_id: str = request.state.get("session_id", "")
        if session_id.strip() == "":
            err_text: str = "Not pass session_id"
            logger.error(err_text)
            raise RuntimeError(err_text)

        nudge_review_skill_count: int = state_register_db.get_state(session_id, "nudge_review_skill_count", 0) + 1
        state_register_db.set_state(session_id, "nudge_review_skill_count", nudge_review_skill_count)
        return await handler(request)

    @override
    async def aafter_agent(
        self, state: StateT, runtime: Runtime[ContextT]
    ) -> dict[str, Any] | None:
        session_id: str = state.get("session_id", "")
        if session_id.strip() == "":
            err_text: str = "Not pass session_id"
            logger.error(err_text)
            raise RuntimeError(err_text)

        messages: list[BaseMessage] = state["messages"]

        system_prompt: str = self._get_and_reload_system_prompt(session_id)
        # nudge memory
        nudge_review_memory_count: int = state_register_db.get_state(session_id, "nudge_review_memory_count", 0) + 1
        nudge_review_skill_count: int = state_register_db.get_state(session_id, "nudge_review_skill_count", 0)

        # if nudge is continute, skip this turn.
        if self._is_lock(session_id):
            state_register_db.set_state(session_id, "nudge_review_memory_count", nudge_review_memory_count)
            return None

        need_nudge_review_memory:bool = nudge_review_memory_count >= 10
        need_nudge_skill_memory:bool = nudge_review_skill_count >= 10

        if need_nudge_skill_memory and need_nudge_review_memory:
            # nudge memory
            state_register_db.set_state(session_id, "nudge_review_memory_count", 0)
            state_register_db.set_state(session_id, "nudge_review_skill_count", 0)

            await _nudge_combined(session_id, system_prompt, messages)
        else:
            if need_nudge_review_memory:
                # nudge memory and skill
                state_register_db.set_state(session_id, "nudge_review_memory_count", 0)
                await _nudge_memory(session_id, system_prompt, messages)
            else:
                state_register_db.set_state(session_id, "nudge_review_memory_count", nudge_review_memory_count)

            if need_nudge_skill_memory:
                # nudge skill
                state_register_db.set_state(session_id, "nudge_review_skill_count", 0)
                await _nudge_skill(session_id, system_prompt, messages)

        return None