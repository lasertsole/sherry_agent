"""Heartbeat staleness detection middleware.

Monitors subagent progress via periodic heartbeat checks. If an agent
shows no progress (iteration count or current tool unchanged) for a
configurable number of heartbeat cycles, it is considered stale and
will be terminated.

Two thresholds control when a stale agent is killed:

* **Idle** (no tool running): tighter threshold — the agent is likely
  stuck on a slow or hung API call.
* **In-tool** (a tool is currently running): looser threshold — the
  agent may be running a legitimately long tool (terminal command,
  web fetch, large file read).

Progress detection
------------------
Every ``heartbeat_interval_seconds`` a background timer fires and
compares the agent's current ``(iteration_count, current_tool)`` pair
against the previously observed values.  If **either** has advanced the
stale counter is reset to zero; otherwise it increments by one.

State storage
-------------
All per-session state is kept in ``state_register_mem`` so it survives
across middleware hooks within the same turn.

Periodic scheduling
-------------------
Uses ``timer_call_register`` to schedule repeating heartbeat checks
on a dedicated background event loop, keeping the main agent loop
unblocked.
"""

from loguru import logger
from typing_extensions import override
from typing import Any, Callable, Awaitable
from langchain_core.messages import AIMessage, ToolMessage
from langgraph.prebuilt.tool_node import ToolCallRequest
from langchain.agents.middleware import AgentMiddleware, AgentState
from langchain.agents.middleware.types import ModelRequest, ModelResponse, ExtendedModelResponse

from runtime import state_register_mem, timer_call_register


_HEARTBEAT_INTERVAL_MINUTES = 1
_STALE_CYCLES_IDLE = 7
_STALE_CYCLES_IN_TOOL = 20

_STATE_KEY_ITER = "heartbeat_iter"
_STATE_KEY_TOOL = "heartbeat_tool"
_STATE_KEY_STALE = "heartbeat_stale"
_STATE_KEY_KILLED = "heartbeat_killed"
_TIMER_NAME = "heartbeat_staleness_check"


class HeartbeatStaleness(AgentMiddleware):
    """Detect and terminate agents that make no progress.

    Parameters
    ----------
    heartbeat_interval_minutes : int
        Minutes between heartbeat checks (1–60).  Default **1**.
    stale_cycles_idle : int
        Consecutive stale cycles before killing an idle agent.  Default **7**
        (7 × 1 min = 7 min idle tolerance).
    stale_cycles_in_tool : int
        Consecutive stale cycles before killing an agent that appears stuck
        inside a tool call.  Default **20** (20 × 1 min = 20 min in-tool
        tolerance).
    """

    def __init__(
        self,
        heartbeat_interval_minutes: int = _HEARTBEAT_INTERVAL_MINUTES,
        stale_cycles_idle: int = _STALE_CYCLES_IDLE,
        stale_cycles_in_tool: int = _STALE_CYCLES_IN_TOOL,
    ):
        super().__init__()
        self.heartbeat_interval_minutes = heartbeat_interval_minutes
        self.stale_cycles_idle = stale_cycles_idle
        self.stale_cycles_in_tool = stale_cycles_in_tool

    def _sid(self, state: AgentState) -> str:
        session_id: str = state.get("session_id", "")
        if not session_id.strip():
            raise RuntimeError("HeartbeatStaleness: session_id is required")
        return session_id

    def _is_killed(self, session_id: str) -> bool:
        return state_register_mem.get_state(session_id, _STATE_KEY_KILLED, False)

    def _check_progress(self, session_id: str) -> None:
        """Heartbeat callback: compare current state with last observed state."""
        if self._is_killed(session_id):
            return

        current_iter: int = state_register_mem.get_state(session_id, _STATE_KEY_ITER, 0)
        current_tool: str | None = state_register_mem.get_state(session_id, _STATE_KEY_TOOL, None)

        last_iter: int = state_register_mem.get_state(session_id, f"_last_{_STATE_KEY_ITER}", 0)
        last_tool: str | None = state_register_mem.get_state(session_id, f"_last_{_STATE_KEY_TOOL}", None)

        iter_advanced = current_iter > last_iter
        tool_changed = current_tool != last_tool

        if iter_advanced or tool_changed:
            state_register_mem.set_state(session_id, f"_last_{_STATE_KEY_ITER}", current_iter)
            state_register_mem.set_state(session_id, f"_last_{_STATE_KEY_TOOL}", current_tool)
            state_register_mem.set_state(session_id, _STATE_KEY_STALE, 0)
            logger.debug(
                "[HeartbeatStaleness] session={} progress detected (iter={}, tool={}), stale reset",
                session_id, current_iter, current_tool,
            )
        else:
            stale: int = state_register_mem.get_state(session_id, _STATE_KEY_STALE, 0) + 1
            state_register_mem.set_state(session_id, _STATE_KEY_STALE, stale)

            stale_limit = self.stale_cycles_in_tool if current_tool else self.stale_cycles_idle
            limit_seconds = stale_limit * self.heartbeat_interval_minutes * 60

            if stale >= stale_limit:
                logger.warning(
                    "[HeartbeatStaleness] session={} appears stale "
                    "(no progress for {} cycles / ~{}s, tool={}) — marking killed",
                    session_id, stale, limit_seconds, current_tool or "<none>",
                )
                state_register_mem.set_state(session_id, _STATE_KEY_KILLED, True)
            else:
                logger.info(
                    "[HeartbeatStaleness] session={} stale count={}/{} (tool={})",
                    session_id, stale, stale_limit, current_tool or "<none>",
                )

    def _start_heartbeat(self, session_id: str) -> None:
        timer_call_register.register(
            session_id=session_id,
            name=_TIMER_NAME,
            callback=self._check_progress,
            args={"session_id": session_id},
            minutes=self.heartbeat_interval_minutes,
            execute_now=True,
        )
        logger.info(
            "[HeartbeatStaleness] started heartbeat for session={} "
            "(interval={}min, idle_cycles={}, tool_cycles={})",
            session_id, self.heartbeat_interval_minutes,
            self.stale_cycles_idle, self.stale_cycles_in_tool,
        )

    def _stop_heartbeat(self, session_id: str) -> None:
        timer_call_register.unregister(session_id, _TIMER_NAME)
        logger.info("[HeartbeatStaleness] stopped heartbeat for session={}", session_id)

    def _before_agent_impl(self, state: AgentState) -> None:
        session_id = self._sid(state)
        state_register_mem.set_state(session_id, _STATE_KEY_ITER, 0)
        state_register_mem.set_state(session_id, _STATE_KEY_TOOL, None)
        state_register_mem.set_state(session_id, _STATE_KEY_STALE, 0)
        state_register_mem.set_state(session_id, _STATE_KEY_KILLED, False)
        state_register_mem.set_state(session_id, f"_last_{_STATE_KEY_ITER}", 0)
        state_register_mem.set_state(session_id, f"_last_{_STATE_KEY_TOOL}", None)
        self._start_heartbeat(session_id)

    def _after_agent_impl(self, state: AgentState) -> None:
        session_id = self._sid(state)
        self._stop_heartbeat(session_id)

    @override
    def before_agent(self, state: AgentState, runtime: Any) -> dict[str, Any] | None:
        self._before_agent_impl(state)
        return None

    @override
    async def abefore_agent(self, state: AgentState, runtime: Any) -> dict[str, Any] | None:
        self._before_agent_impl(state)
        return None

    @override
    def after_agent(self, state: AgentState, runtime: Any) -> dict[str, Any] | None:
        self._after_agent_impl(state)
        return None

    @override
    async def aafter_agent(self, state: AgentState, runtime: Any) -> dict[str, Any] | None:
        self._after_agent_impl(state)
        return None

    def _wrap_model_call_impl(self, request: ModelRequest) -> AIMessage | None:
        session_id = self._sid(request.state)

        if self._is_killed(session_id):
            stale_limit = self.stale_cycles_idle
            tolerance_seconds = stale_limit * self.heartbeat_interval_minutes * 60
            logger.warning(
                "[HeartbeatStaleness] session={} killed — returning terminal message",
                session_id,
            )
            return AIMessage(
                content=(
                    f"Heartbeat staleness timeout exceeded (~{tolerance_seconds}s idle, "
                    f"~{self.stale_cycles_in_tool * self.heartbeat_interval_minutes * 60}s in-tool). "
                    "I must stop here. Please summarize what has been accomplished "
                    "and what remains to be done."
                )
            )

        current_iter: int = state_register_mem.get_state(session_id, _STATE_KEY_ITER, 0)
        state_register_mem.set_state(session_id, _STATE_KEY_ITER, current_iter + 1)

        return None

    @override
    def wrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], ModelResponse],
    ) -> ModelResponse | AIMessage | ExtendedModelResponse:
        terminal = self._wrap_model_call_impl(request)
        if terminal is not None:
            return terminal
        return handler(request)

    @override
    async def awrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], Awaitable[ModelResponse]],
    ) -> ModelResponse | AIMessage | ExtendedModelResponse:
        terminal = self._wrap_model_call_impl(request)
        if terminal is not None:
            return terminal
        return await handler(request)

    def _wrap_tool_call_impl(self, request: ToolCallRequest) -> ToolMessage | None:
        session_id = self._sid(request.state)

        if self._is_killed(session_id):
            tool_name: str = request.tool_call.get("name", "unknown")
            logger.warning(
                "[HeartbeatStaleness] session={} killed during tool call [{}]",
                session_id, tool_name,
            )
            return ToolMessage(
                content=(
                    f"Tool [{tool_name}] skipped — heartbeat staleness timeout exceeded. "
                    "No further actions can be taken."
                ),
                tool_call_id=request.tool_call["id"],
                name=tool_name,
                status="error",
            )

        tool_name = request.tool_call.get("name", "unknown")
        state_register_mem.set_state(session_id, _STATE_KEY_TOOL, tool_name)

        return None

    def _after_tool_call_impl(self, request: ToolCallRequest) -> None:
        session_id = self._sid(request.state)
        state_register_mem.set_state(session_id, _STATE_KEY_TOOL, None)

    @override
    def wrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], ToolMessage],
    ) -> ToolMessage:
        terminal = self._wrap_tool_call_impl(request)
        if terminal is not None:
            return terminal
        result = handler(request)
        self._after_tool_call_impl(request)
        return result

    @override
    async def awrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], Awaitable[ToolMessage]],
    ) -> ToolMessage:
        terminal = self._wrap_tool_call_impl(request)
        if terminal is not None:
            return terminal
        result = await handler(request)
        self._after_tool_call_impl(request)
        return result
