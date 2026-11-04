"""Per-turn model-call budget for an agent.

Each agent instance holds an :class:`IterationBudget`;
the cap comes from ``max_iterations`` (default 50). One unit is charged per
MODEL call (the call that produces an AIMessage); a tool-call pair costs one
unit total because its ToolMessage result is free.

When the budget is exhausted the middleware returns a terminal
``AIMessage`` instead of calling the model, forcing the conversation to
a graceful stop.
"""

from __future__ import annotations

from loguru import logger
from langgraph.typing import ContextT
from typing import override
from collections.abc import Callable, Awaitable
from langchain_core.messages import AIMessage
from langchain.agents.middleware import AgentMiddleware, AgentState
from langchain.agents.middleware.types import (
    ResponseT,
    ModelRequest,
    ModelResponse,
    ExtendedModelResponse,
)

from runtime import StateKey, state_register_mem
from config.features import ITERATION_BUDGET

from agent.middlewares.base import BeforeAgentHooksMixin, require_session_id
from agent.middlewares.subagent_completion_drain.core import _is_internal_completion


class IterationBudget(BeforeAgentHooksMixin, AgentMiddleware):
    """Enforce a hard cap on model-call iterations per turn.

    One unit is charged per MODEL call (the call that produces an AIMessage),
    never per stored message. A tool-call pair therefore costs exactly one
    unit: the AIMessage carrying the call, while its ToolMessage result is
    free. System reminders, HumanMessages (including queue/continuation
    carriers) and SystemMessages never charge the counter on their own — they
    are simply part of the prompt the next model call sees.

    The budget counter is stored in ``state_register_mem`` under the key
    ``iteration_budget`` so that it survives across middleware hooks within the
    same turn but is cheap to reset between turns via ``abefore_agent``.

    Parameters
    ----------
    max_iterations : int
        Maximum number of model calls allowed in a single turn. Default **50**.
    """

    _BUDGET_KEY = StateKey.ITERATION_BUDGET
    _USED_KEY = StateKey.ITERATION_BUDGET_USED

    def __init__(self, max_iterations: int = ITERATION_BUDGET["default_max_iterations"]):
        super().__init__()
        self.max_iterations = max_iterations

    def _get_session_id(self, state: AgentState) -> str:
        return require_session_id(state, "IterationBudget: session_id is required")

    def _consume(self, session_id: str) -> bool:
        logger.debug("[IB_TRACE] _consume enter session_id={}", session_id)
        used: int = state_register_mem.get_state(session_id, self._USED_KEY, 0)
        if used >= self.max_iterations:
            logger.debug(
                "[IB_TRACE] _consume exhausted session_id={} used={} max={}",
                session_id,
                used,
                self.max_iterations,
            )
            return False
        used += 1
        remaining = self.max_iterations - used
        state_register_mem.set_state(session_id, self._USED_KEY, used)
        logger.debug(
            "session_id {} consume {} times, {} remaining before halt", session_id, used, remaining
        )
        logger.debug(
            "[IB_TRACE] _consume exit session_id={} used={} remaining={}",
            session_id,
            used,
            remaining,
        )
        return True

    def _remaining(self, session_id: str) -> int:
        used: int = state_register_mem.get_state(session_id, self._USED_KEY, 0)
        return max(0, self.max_iterations - used)

    def _before_agent_impl(self, state: AgentState) -> None:
        session_id = self._get_session_id(state)
        state_register_mem.set_state(session_id, self._BUDGET_KEY, self.max_iterations)
        state_register_mem.set_state(session_id, self._USED_KEY, 0)

    def _wrap_model_call_impl(
        self,
        request: ModelRequest[ContextT],
    ) -> AIMessage | None:
        """Check budget and return terminal AIMessage if exhausted, or None to proceed."""
        # internal completion-notification turns are not paid iterations.
        if _is_internal_completion((request.state.get("messages") or [None])[-1]):
            return None
        session_id = self._get_session_id(request.state)

        if not self._consume(session_id):
            remaining = self._remaining(session_id)
            logger.warning(
                "IterationBudget exhausted for session {} (remaining={}, max={}). "
                "Returning terminal message instead of calling model.",
                session_id,
                remaining,
                self.max_iterations,
            )
            return AIMessage(
                content=(
                    f"The iteration budget ({self.max_iterations}) has been exhausted. "
                    "I must stop here. Please summarize what has been accomplished "
                    "and what remains to be done."
                )
            )

        return None

    @override
    def wrap_model_call(
        self,
        request: ModelRequest[ContextT],
        handler: Callable[[ModelRequest[ContextT]], ModelResponse[ResponseT]],
    ) -> ModelResponse[ResponseT] | AIMessage | ExtendedModelResponse[ResponseT]:
        logger.bind(middleware=type(self).__name__).debug("wrap_model_call hook fired")
        session_id = self._get_session_id(request.state)
        logger.debug("[IB_TRACE] wrap_model_call enter session_id={}", session_id)
        terminal = self._wrap_model_call_impl(request)
        if terminal is not None:
            logger.debug(
                "[IB_TRACE] wrap_model_call terminal (budget exhausted) session_id={}", session_id
            )
            return terminal
        logger.debug("[IB_TRACE] wrap_model_call calling inner handler session_id={}", session_id)
        result = handler(request)
        logger.debug("[IB_TRACE] wrap_model_call inner handler returned session_id={}", session_id)
        return result

    @override
    async def awrap_model_call(
        self,
        request: ModelRequest[ContextT],
        handler: Callable[[ModelRequest[ContextT]], Awaitable[ModelResponse[ResponseT]]],
    ) -> ModelResponse[ResponseT] | AIMessage | ExtendedModelResponse[ResponseT]:
        logger.bind(middleware=type(self).__name__).debug("awrap_model_call hook fired")
        terminal = self._wrap_model_call_impl(request)
        if terminal is not None:
            return terminal
        return await handler(request)
