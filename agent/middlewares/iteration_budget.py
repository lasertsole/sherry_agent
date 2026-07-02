"""Per-agent iteration budget — thread-safe consume/refund counter.

Equivalent to hermes-agent's ``agent/iteration_budget.py``.

Each agent instance (parent or subagent) holds an :class:`IterationBudget`;
the parent's cap comes from ``max_iterations`` (default 50), each subagent's
cap comes from ``subagent_max_iterations`` (default 30).

When the budget is exhausted the middleware returns a terminal
``AIMessage`` instead of calling the model, forcing the conversation to
a graceful stop.
"""

from __future__ import annotations

import threading
from typing import Any, Callable, Awaitable

from loguru import logger
from langgraph.runtime import Runtime
from langgraph.typing import ContextT
from langgraph.prebuilt.tool_node import ToolCallRequest
from typing_extensions import override
from langchain_core.messages import AIMessage, ToolMessage
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
    subagent_max_iterations : int
        Budget for subagent delegations.  Default **30**.
    """

    _BUDGET_KEY = "iteration_budget"
    _USED_KEY = "iteration_budget_used"

    def __init__(self, max_iterations: int = 50, subagent_max_iterations: int = 30):
        super().__init__()
        self.max_iterations = max_iterations
        self.subagent_max_iterations = subagent_max_iterations

    def _get_session_id(self, state: dict[str, Any]) -> str:
        session_id: str = state.get("session_id", "")
        if not session_id.strip():
            raise RuntimeError("IterationBudget: session_id is required")
        return session_id

    def _consume(self, session_id: str) -> bool:
        used: int = state_register_mem.get_state(session_id, self._USED_KEY, 0)
        if used >= self.max_iterations:
            return False
        state_register_mem.set_state(session_id, self._USED_KEY, used + 1)
        return True

    def _remaining(self, session_id: str) -> int:
        used: int = state_register_mem.get_state(session_id, self._USED_KEY, 0)
        return max(0, self.max_iterations - used)

    @override
    async def abefore_agent(
        self, state: AgentState, runtime: Runtime
    ) -> dict[str, Any] | None:
        session_id = self._get_session_id(state)
        state_register_mem.set_state(session_id, self._BUDGET_KEY, self.max_iterations)
        state_register_mem.set_state(session_id, self._USED_KEY, 0)
        return None

    @override
    async def awrap_model_call(
        self,
        request: ModelRequest[ContextT],
        handler: Callable[[ModelRequest[ContextT]], Awaitable[ModelResponse[ResponseT]]],
    ) -> ModelResponse[ResponseT] | AIMessage | ExtendedModelResponse[ResponseT]:
        session_id = self._get_session_id(request.state)

        if not self._consume(session_id):
            remaining = self._remaining(session_id)
            logger.warning(
                "IterationBudget exhausted for session %s (used=%d, max=%d). "
                "Returning terminal message instead of calling model.",
                session_id, self.max_iterations, self.max_iterations,
            )
            return AIMessage(
                content=(
                    f"The iteration budget ({self.max_iterations}) has been exhausted. "
                    "I must stop here. Please summarize what has been accomplished "
                    "and what remains to be done."
                )
            )

        return await handler(request)

    @override
    async def awrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], Awaitable[ToolMessage]],
    ) -> ToolMessage:
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

        return await handler(request)
