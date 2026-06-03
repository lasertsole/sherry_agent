from typing import Any, Callable, Awaitable

from langchain_core.messages import ToolMessage
from langgraph.prebuilt.tool_node import ToolCallRequest
from langgraph.types import Command
from typing_extensions import override
from langchain.agents.middleware import AgentMiddleware, AgentState
from langgraph.runtime import Runtime


class ToolLoopPrevention(AgentMiddleware):
    """Prevent the same tool from being called more than N times in a single
    conversation turn.  Counter resets at the start of each new turn via
    ``abefore_agent``.

    Threshold (default: 7) — once a tool's call count exceeds this in one
    turn, subsequent calls to that tool are silently skipped and a warning
    ``ToolMessage`` is returned instead.
    """

    def __init__(self, session_id: str, threshold: int = 7, **kwargs):
        super().__init__(**kwargs)

        self._session_id: str = session_id
        self._threshold: int = threshold
        # tool_name -> call_count for the current turn
        self._turn_tool_counts: dict[str, int] = {}

    @override
    async def abefore_agent(
        self, state: AgentState, runtime: Runtime
    ) -> dict[str, Any] | None:
        """Reset per-turn counters at the start of each new conversation turn."""
        self._turn_tool_counts.clear()
        return None

    @override
    async def awrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], Awaitable[ToolMessage | Command[Any]]],
    ) -> ToolMessage | Command[Any]:
        tool_name: str = request.tool_call["name"]
        count: int = self._turn_tool_counts.get(tool_name, 0) + 1
        self._turn_tool_counts[tool_name] = count

        if count > self._threshold:
            return ToolMessage(
                content=(
                    f"Tool [{tool_name}] has been called {count} times in this "
                    f"turn, exceeding the limit of {self._threshold}. "
                    "Its execution has been skipped. "
                    "Please reconsider your approach."
                ),
                tool_call_id=request.tool_call["id"],
                name=tool_name,
                status="error",
            )

        return await handler(request)
