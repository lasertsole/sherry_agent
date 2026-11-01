"""Main-agent delegation-pressure gate (8-read warn, 16-read read/write block).

The counter is cumulative for ONE turn and resets only in ``after_agent``.
At ``warn_after`` the main agent receives one directive to delegate; at
``block_after`` every read/write tool call is refused for the rest of the turn
with an error pointing at ``sessions_spawn``. Spawning does NOT unlock the
turn and writes do NOT reset the counter.

Browser debugging tools are excluded from both counting and blocking: they are
neither reads nor writes under this pressure model. The middleware is installed
on the main agent only; ``_active`` also rejects child/swarm sessions
defensively.
"""

from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass
from typing import Any
from collections.abc import Awaitable, Callable

from langchain.agents.middleware import AgentMiddleware, AgentState
from langchain_core.messages import HumanMessage, ToolMessage
from langgraph.prebuilt.tool_node import ToolCallRequest
from loguru import logger

from config.features import DELEGATION_PRESSURE
from runtime import state_register_mem

from agent.middlewares.base import (
    AfterAgentHooksMixin,
    BeforeAgentHooksMixin,
    require_session_id,
)

__all__ = ["DelegationPressureGate"]

_STATE_KEY = "delegation_pressure_state"


@dataclass
class _PressureState:
    read_calls: int = 0
    warned: bool = False
    blocked: bool = False
    block_injected: bool = False


def _active(session_id: str) -> bool:
    if not session_id:
        return False
    # Never active for a child, even if a future builder accidentally adds the
    # middleware to the child chain: the gate is a MAIN-agent contract.
    if ":subagent:" in session_id or ":swarm:" in session_id:
        return False
    # The hard arm supersedes the soft one when both env flags are present.
    if os.getenv("SHERRY_EVAL_DELEGATE_ONLY") == "1":
        return False
    if os.getenv("SHERRY_EVAL_DELEGATION_PRESSURE") == "1" and session_id.startswith("evals-swe-"):
        return True
    return bool(DELEGATION_PRESSURE["enabled"])


class DelegationPressureGate(AfterAgentHooksMixin, BeforeAgentHooksMixin, AgentMiddleware):
    """Warn at 8 reads, then block read/write tools for the rest of the turn."""

    def __init__(self) -> None:
        super().__init__()
        self._pressure_tools = frozenset(DELEGATION_PRESSURE["pressure_tools"])
        self._blocked_tools = frozenset(DELEGATION_PRESSURE["blocked_tools"])
        self._excluded_tools = frozenset(DELEGATION_PRESSURE["excluded_tools"])

    @staticmethod
    def _session_id(state: AgentState) -> str:
        return require_session_id(state, "DelegationPressureGate: session_id is required")

    def _get(self, session_id: str) -> _PressureState:
        state = state_register_mem.get_state(session_id, _STATE_KEY, None)
        return state if isinstance(state, _PressureState) else _PressureState()

    def _save(self, session_id: str, state: _PressureState) -> None:
        state_register_mem.set_state(session_id, _STATE_KEY, state)

    # ── turn lifecycle ──────────────────────────────────────────────────────
    def _before_agent_impl(self, state: AgentState) -> None:
        # No reset here: the counter is cumulative across every graph
        # invocation inside one turn (including HITL resumes). after_agent owns
        # the reset. A missing state naturally starts at zero.
        return

    def _after_agent_impl(self, state: AgentState) -> None:
        try:
            session_id = self._session_id(state)
            if not _active(session_id):
                return
            self._save(session_id, _PressureState())
        except Exception:  # noqa: BLE001 - the turn-end hook must never fail a turn
            logger.exception("DelegationPressureGate: after_agent failed; continuing")

    # ── messages ────────────────────────────────────────────────────────────
    def _directive(self, content: str) -> HumanMessage:
        return HumanMessage(
            content=content,
            metadata={
                "origin": "delegation_pressure",
                "internal": True,
                "provenance": "delegation_pressure",
            },
        )

    def _warn_message(self, calls: int) -> str:
        return (
            "[SYSTEM DIRECTIVE: DELEGATION PRESSURE]\n\n"
            f"You have made {calls} read/exploration calls this turn.\n"
            f"At {DELEGATION_PRESSURE['block_after']} read calls, read and write tools "
            "will be disabled for the rest of the turn.\n"
            "Delegate the remaining work now: call `sessions_spawn` with\n"
            "functional_role='researcher' for read-only research, or 'executor' for\n"
            "implementation/test work."
        )

    def _block_message(self) -> str:
        return (
            "[SYSTEM DIRECTIVE: DELEGATION PRESSURE — READ/WRITE DISABLED]\n\n"
            f"{DELEGATION_PRESSURE['block_after']} read calls were reached; every "
            "read/write tool is paused for the rest of this turn.\n"
            "Use `sessions_spawn` now: functional_role='researcher' for read-only\n"
            "research, 'executor' for implementation/test work. Then use\n"
            "`sessions_messages` / `subagents_list` / `sessions_yield` to collect the\n"
            "result. Browser debugging tools remain available."
        )

    def _refusal(self, request: ToolCallRequest, tool_name: str) -> ToolMessage:
        return ToolMessage(
            content=(
                "Delegation pressure: read/write tools are disabled for this turn "
                f"after {DELEGATION_PRESSURE['block_after']} read calls. "
                "Use `sessions_spawn` (researcher for read-only research, executor for "
                "implementation) and collect its result with `sessions_messages` / "
                "`subagents_list` / `sessions_yield`. "
                f"`{tool_name}` remains unavailable until the next turn."
            ),
            tool_call_id=str(request.tool_call.get("id", "")),
            name=tool_name,
            status="error",
        )

    def _excluded(self, name: str) -> bool:
        return name in self._excluded_tools or name.startswith("browser_")

    # ── tool hooks ──────────────────────────────────────────────────────────
    def _pre_tool(self, request: ToolCallRequest) -> ToolMessage | None:
        session_id = str((request.state or {}).get("session_id") or "")
        if not _active(session_id):
            return None
        name = str(request.tool_call.get("name") or "")
        if self._excluded(name):
            return None
        state = self._get(session_id)
        if state.blocked:
            if name in self._blocked_tools:
                return self._refusal(request, name)
            return None
        if name in self._pressure_tools:
            state.read_calls += 1
            self._save(session_id, state)
        return None

    def _post_tool(self, request: ToolCallRequest, result: Any) -> None:
        session_id = str((request.state or {}).get("session_id") or "")
        if not _active(session_id):
            return
        name = str(request.tool_call.get("name") or "")
        if self._excluded(name) or name not in self._pressure_tools:
            return
        state = self._get(session_id)
        if state.blocked:
            return
        if state.read_calls >= DELEGATION_PRESSURE["block_after"]:
            state.blocked = True
            self._save(session_id, state)
            logger.warning(
                "DelegationPressureGate: read/write blocked session={} reads={}",
                session_id,
                state.read_calls,
            )

    def wrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], Any],
    ) -> Any:
        try:
            blocked = self._pre_tool(request)
        except Exception:  # noqa: BLE001 - the gate must never break a turn
            logger.exception("DelegationPressureGate: sync precheck failed; continuing")
            blocked = None
        if blocked is not None:
            return blocked
        result = handler(request)
        try:
            self._post_tool(request, result)
        except Exception:  # noqa: BLE001
            logger.exception("DelegationPressureGate: sync postcheck failed; continuing")
        return result

    async def awrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], Awaitable[Any]],
    ) -> Any:
        try:
            blocked = self._pre_tool(request)
        except Exception:  # noqa: BLE001 - the gate must never break a turn
            logger.exception("DelegationPressureGate: async precheck failed; continuing")
            blocked = None
        if blocked is not None:
            return blocked
        result = await handler(request)
        try:
            self._post_tool(request, result)
        except Exception:  # noqa: BLE001
            logger.exception("DelegationPressureGate: async postcheck failed; continuing")
        return result

    async def abefore_model(self, state: AgentState, runtime: Any = None) -> dict[str, Any] | None:
        """Inject the one-time warning or the blocked directive."""
        try:
            session_id = self._session_id(state)
            if not _active(session_id):
                return None
            gate = self._get(session_id)
            if gate.blocked and not gate.block_injected:
                gate.block_injected = True
                self._save(session_id, gate)
                return {"messages": [self._directive(self._block_message())]}
            if (
                not gate.blocked
                and gate.read_calls >= DELEGATION_PRESSURE["warn_after"]
                and not gate.warned
            ):
                gate.warned = True
                self._save(session_id, gate)
                return {"messages": [self._directive(self._warn_message(gate.read_calls))]}
        except Exception:  # noqa: BLE001
            logger.exception("DelegationPressureGate: before_model failed; continuing")
        return None

    def before_model(self, state: AgentState, runtime: Any = None) -> dict[str, Any] | None:
        """Sync best-effort path; the streaming main agent uses ``abefore_model``."""
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(self.abefore_model(state, runtime))
        return None
