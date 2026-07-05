"""Per-agent iteration budget — consume/refund counter.

Each agent instance holds an :class:`IterationBudget`;
the cap comes from ``max_iterations`` (default 50).

When the budget is exhausted the middleware returns a terminal
``AIMessage`` instead of calling the model, forcing the conversation to
a graceful stop.
"""

from __future__ import annotations

from loguru import logger
from langgraph.runtime import Runtime
from langgraph.typing import ContextT
from typing_extensions import override
from typing import Any, Callable, Awaitable
from langchain_core.messages import AIMessage, ToolMessage
from langgraph.prebuilt.tool_node import ToolCallRequest, Command
from langchain.agents.middleware import AgentMiddleware, AgentState
from langchain.agents.middleware.types import ResponseT, ModelRequest, ModelResponse, ExtendedModelResponse

from runtime import state_register_mem


class IterationBudget(AgentMiddleware):
    """Enforce a hard cap on the total number of model + tool iterations per turn.

    The budget counter is stored in ``state_register_mem`` under the key
    ``iteration_budget`` so that it survives across middleware hooks within the
    same turn but is cheap to reset between turns via ``abefore_agent``.

    Parameters
    ----------
    max_iterations : int
        Maximum number of combined model-call + tool-call iterations allowed
        in a single turn.  Default **50**.
    """

    _BUDGET_KEY = "iteration_budget"
    _USED_KEY = "iteration_budget_used"

    def __init__(self, max_iterations: int = 50):
        super().__init__()
        self.max_iterations = max_iterations

    def _get_session_id(self, state: AgentState) -> str:
        session_id: str = state.get("session_id", "")
        if not session_id.strip():
            raise RuntimeError("IterationBudget: session_id is required")
        return session_id

    def _consume(self, session_id: str) -> bool:
        logger.info("[IB_TRACE] _consume enter session_id={}", session_id)
        used: int = state_register_mem.get_state(session_id, self._USED_KEY, 0)
        if used >= self.max_iterations:
            logger.info("[IB_TRACE] _consume exhausted session_id={} used={} max={}", session_id, used, self.max_iterations)
            return False
        used += 1
        remaining = self.max_iterations - used
        state_register_mem.set_state(session_id, self._USED_KEY, used)
        logger.info("session_id {} consume {} times, {} remaining before halt", session_id, used, remaining)
        logger.info("[IB_TRACE] _consume exit session_id={} used={} remaining={}", session_id, used, remaining)
        return True

    def _remaining(self, session_id: str) -> int:
        used: int = state_register_mem.get_state(session_id, self._USED_KEY, 0)
        return max(0, self.max_iterations - used)

    def _before_agent_impl(self, state: AgentState) -> None:
        session_id = self._get_session_id(state)
        state_register_mem.set_state(session_id, self._BUDGET_KEY, self.max_iterations)
        state_register_mem.set_state(session_id, self._USED_KEY, 0)

    @override
    def before_agent(
        self, state: AgentState, runtime: Runtime[ContextT]
    ) -> dict[str, Any] | None:
        self._before_agent_impl(state)
        return None

    @override
    async def abefore_agent(
        self, state: AgentState, runtime: Runtime[ContextT]
    ) -> dict[str, Any] | None:
        self._before_agent_impl(state)
        return None

    def _wrap_model_call_impl(
        self,
        request: ModelRequest[ContextT],
    ) -> AIMessage | None:
        """Check budget and return terminal AIMessage if exhausted, or None to proceed."""
        session_id = self._get_session_id(request.state)

        if not self._consume(session_id):
            remaining = self._remaining(session_id)
            logger.warning(
                "IterationBudget exhausted for session {} (remaining={}, max={}). "
                "Returning terminal message instead of calling model.",
                session_id, remaining, self.max_iterations,
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
        session_id = self._get_session_id(request.state)
        logger.info("[IB_TRACE] wrap_model_call enter session_id={}", session_id)
        terminal = self._wrap_model_call_impl(request)
        if terminal is not None:
            logger.info("[IB_TRACE] wrap_model_call terminal (budget exhausted) session_id={}", session_id)
            return terminal
        logger.info("[IB_TRACE] wrap_model_call calling inner handler session_id={}", session_id)
        result = handler(request)
        logger.info("[IB_TRACE] wrap_model_call inner handler returned session_id={}", session_id)
        return result

    @override
    async def awrap_model_call(
        self,
        request: ModelRequest[ContextT],
        handler: Callable[[ModelRequest[ContextT]], Awaitable[ModelResponse[ResponseT]]],
    ) -> ModelResponse[ResponseT] | AIMessage | ExtendedModelResponse[ResponseT]:
        terminal = self._wrap_model_call_impl(request)
        if terminal is not None:
            return terminal
        return await handler(request)

    def _wrap_tool_call_impl(
        self,
        request: ToolCallRequest,
    ) -> ToolMessage | None:
        """Check budget and return terminal ToolMessage if exhausted, or None to proceed."""
        session_id = self._get_session_id(request.state)

        if not self._consume(session_id):
            tool_name: str = request.tool_call.get("name", "unknown")
            logger.warning(
                "IterationBudget exhausted during tool call [%s] for session %s.",
                tool_name, session_id,
            )
            return ToolMessage(
                content=(
                    f"Tool [{tool_name}] skipped — iteration budget "
                    f"({self.max_iterations}) exhausted. "
                    "No further actions can be taken this turn."
                ),
                tool_call_id=request.tool_call["id"],
                name=tool_name,
                status="error",
            )

        return None

    @override
    def wrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], ToolMessage | Command[Any]],
    ) -> ToolMessage | Command[Any]:
        terminal = self._wrap_tool_call_impl(request)
        if terminal is not None:
            return terminal
        return handler(request)

    @override
    async def awrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], Awaitable[ToolMessage | Command[Any]]],
    ) -> ToolMessage | Command[Any]:
        terminal = self._wrap_tool_call_impl(request)
        if terminal is not None:
            return terminal
        return await handler(request)
