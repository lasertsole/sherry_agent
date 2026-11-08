"""Main-agent delegation-pressure gate (7-read warn, 12-read escalated warn).

The counter is cumulative for ONE turn. At ``warn_after`` the main agent
receives one system-reminder directive to delegate; at ``block_after`` it
receives an escalated system-reminder with stronger language. Tools remain
available at both levels — the gate advises, never blocks. A successful spawn
(accepted or pending) resets the counter so the main agent can inspect/verify
and a later 7/12 cycle re-arms; a failed spawn does not reset. Writes alone do
not reset the counter. ``after_agent`` also resets it at the turn boundary.

The directive uses ``wrap_reminder`` / ``system_reminder_metadata`` so the
summarizer and the reminder scanners classify it as a system notice, not as a
user message. Browser debugging tools are excluded from counting: they are
neither reads nor writes under this pressure model. The middleware is installed
on the main agent only; ``_active`` also rejects child/swarm sessions
defensively.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from langchain.agents.middleware import AgentMiddleware, AgentState
from langchain_core.messages import HumanMessage, ToolMessage
from langgraph.prebuilt.tool_node import ToolCallRequest
from loguru import logger

from agent.middlewares.base import (
    AfterAgentHooksMixin,
    BeforeAgentHooksMixin,
    require_session_id,
)
from config.features import DELEGATION_PRESSURE
from pub.func.message.system_reminder import system_reminder_metadata, wrap_reminder
from runtime import state_register_mem

__all__ = ["DelegationPressureGate"]

_STATE_KEY = "delegation_pressure_state"


@dataclass
class _PressureState:
    read_calls: int = 0
    warned: bool = False
    escalated: bool = False


def _active(session_id: str) -> bool:
    if not session_id:
        return False
    # Never active for a child, even if a future builder accidentally adds the
    # middleware to the child chain: the gate is a MAIN-agent contract.
    if ":subagent:" in session_id or ":swarm:" in session_id:
        return False
    if os.getenv("SHERRY_EVAL_DELEGATION_PRESSURE") == "1" and session_id.startswith("evals-swe-"):
        return True
    return bool(DELEGATION_PRESSURE["enabled"])


class DelegationPressureGate(AfterAgentHooksMixin, BeforeAgentHooksMixin, AgentMiddleware):
    """Warn at 7 reads, escalate at 12, and reset after an accepted spawn."""

    def __init__(self) -> None:
        super().__init__()
        self._pressure_tools = frozenset(DELEGATION_PRESSURE["pressure_tools"])
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

    # ── directives ──────────────────────────────────────────────────────────
    def _directive(self, content: str) -> HumanMessage:
        return HumanMessage(
            content=wrap_reminder("delegation_pressure", content),
            metadata=system_reminder_metadata("delegation_pressure"),
        )

    def _warn_message(self, calls: int) -> str:
        return (
            f"You have made {calls} read/exploration calls this turn.\n"
            "Consider delegating the remaining work: call `sessions_spawn` with\n"
            "functional_role='researcher' for read-only research, or 'executor' for\n"
            "implementation/test work. Tools remain available."
        )

    def _escalated_message(self, calls: int) -> str:
        return (
            "[DELEGATION PRESSURE — ESCALATED]\n\n"
            f"You have made {calls} read/exploration calls this turn — well beyond\n"
            "what a main agent should spend on exploration before delegating.\n"
            "Delegate NOW: call `sessions_spawn` with functional_role='researcher'\n"
            "for read-only research, or 'executor' for implementation/test work.\n"
            "Then use `sessions_messages` / `subagents_list` / `sessions_yield` to\n"
            "collect the result. Tools remain available, but continuing to read\n"
            "instead of delegating will waste the turn's time budget."
        )

    def _excluded(self, name: str) -> bool:
        return name in self._excluded_tools or name.startswith("browser_")

    @staticmethod
    def _spawn_accepted(result: Any) -> bool:
        """True only for a positively accepted/pending ``sessions_spawn`` result.

        A failed spawn must not release the pressure: the model still has no
        delegated work and should correct the spawn contract instead of
        returning to its read loop.
        """
        status = str(getattr(result, "status", "") or "").lower()
        if status in {"error", "failed"}:
            return False
        content = getattr(result, "content", "")
        if isinstance(content, list):
            content = " ".join(
                str(part.get("text", "")) if isinstance(part, dict) else str(part)
                for part in content
            )
        text = str(content or "").lower()
        return any(
            marker in text
            for marker in (
                "subagent spawned",
                "status=accepted",
                "status=pending",
            )
        )

    # ── tool hooks ──────────────────────────────────────────────────────────
    def _pre_tool(self, request: ToolCallRequest) -> ToolMessage | None:
        session_id = str((request.state or {}).get("session_id") or "")
        if not _active(session_id):
            return None
        name = str(request.tool_call.get("name") or "")
        if self._excluded(name):
            return None
        state = self._get(session_id)
        if name in self._pressure_tools:
            state.read_calls += 1
            self._save(session_id, state)
        return None  # never refuse — the gate advises, never blocks

    def _post_tool(self, request: ToolCallRequest, result: Any) -> None:
        session_id = str((request.state or {}).get("session_id") or "")
        if not _active(session_id):
            return
        name = str(request.tool_call.get("name") or "")
        if name == "sessions_spawn" and self._spawn_accepted(result):
            # A successful hand-off is the gate's purpose. Restart the count so
            # the main agent can inspect/verify; a later 7/12 cycle re-arms.
            # A failed spawn is not a hand-off and does not reset.
            self._save(session_id, _PressureState())
            logger.info(
                "DelegationPressureGate: accepted spawn reset the pressure count session={}",
                session_id,
            )

    def wrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], Any],
    ) -> Any:
        try:
            self._pre_tool(request)
        except Exception:  # noqa: BLE001 - the gate must never break a turn
            logger.exception("DelegationPressureGate: sync precheck failed; continuing")
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
            self._pre_tool(request)
        except Exception:  # noqa: BLE001 - the gate must never break a turn
            logger.exception("DelegationPressureGate: async precheck failed; continuing")
        result = await handler(request)
        try:
            self._post_tool(request, result)
        except Exception:  # noqa: BLE001
            logger.exception("DelegationPressureGate: async postcheck failed; continuing")
        return result

    async def abefore_model(self, state: AgentState, runtime: Any = None) -> dict[str, Any] | None:
        """Inject the one-time warning or escalated system reminder."""
        try:
            session_id = self._session_id(state)
            if not _active(session_id):
                return None
            gate = self._get(session_id)
            if gate.read_calls >= DELEGATION_PRESSURE["block_after"] and not gate.escalated:
                gate.escalated = True
                self._save(session_id, gate)
                return {"messages": [self._directive(self._escalated_message(gate.read_calls))]}
            if (
                not gate.escalated
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
