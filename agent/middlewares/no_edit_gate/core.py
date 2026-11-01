"""No-edit gate (eval-only).

The SWE/coding harness needs a hard convergence signal: a turn that has spent
most of its budget reading/searching without writing a patch must stop
researching and produce a minimal edit. This middleware is deliberately NOT
part of the normal product chain: ``agent/core.py`` appends it only when
``SHERRY_EVAL_REQUIRE_EDIT=1``, and the SWE suite sets that env plus uses
``evals-swe-*`` session ids. Pure exploration/chat sessions never see it.

Policy:

* warn once after 15 tool calls without a write;
* at 70% of the turn's iteration budget with no write, enter the hard state:
  only ``read_file`` and the write tools remain usable; terminal/search/web/
  researcher-spawn calls are refused with an error until a patch is written;
* ``patch_file`` / ``write_file`` (or a write-capable ``sessions_spawn``) resets
  the gate.
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

from runtime import StateKey, state_register_mem

from agent.middlewares.base import BeforeAgentHooksMixin, require_session_id

__all__ = ["NoEditGate"]

_WRITE_TOOLS = frozenset({"patch_file", "write_file"})
_READ_ONLY_ROLES = frozenset({"researcher", "librarian"})
_ALLOWED_AFTER_HARD = frozenset({"patch_file", "write_file", "read_file"})
_WARN_AFTER_CALLS = 15
_HARD_RATIO = 0.70
_STATE_KEY = "no_edit_gate_state"


@dataclass
class _GateState:
    calls: int = 0
    writes: int = 0
    warned: bool = False
    hard: bool = False


def _active(session_id: str) -> bool:
    return os.getenv("SHERRY_EVAL_REQUIRE_EDIT") == "1" and session_id.startswith("evals-swe-")


class NoEditGate(BeforeAgentHooksMixin, AgentMiddleware):
    """Force a patch after a long no-write stretch (eval-only)."""

    @staticmethod
    def _session_id(state: AgentState) -> str:
        return require_session_id(state, "NoEditGate: session_id is required")

    def _get(self, session_id: str) -> _GateState:
        return state_register_mem.get_state(session_id, _STATE_KEY, _GateState())

    def _save(self, session_id: str, gate: _GateState) -> None:
        state_register_mem.set_state(session_id, _STATE_KEY, gate)

    def _before_agent_impl(self, state: AgentState) -> None:
        session_id = self._session_id(state)
        if not _active(session_id):
            return
        self._save(session_id, _GateState())

    @staticmethod
    def _is_write_path(tool_name: str, args: Any) -> bool:
        if tool_name in _WRITE_TOOLS:
            return True
        if tool_name == "sessions_spawn" and isinstance(args, dict):
            return args.get("functional_role") not in _READ_ONLY_ROLES
        return False

    @staticmethod
    def _directive(content: str) -> HumanMessage:
        return HumanMessage(
            content=content,
            metadata={"origin": "no_edit_gate", "internal": True, "provenance": "no_edit_gate"},
        )

    def _warn_message(self, calls: int) -> str:
        return (
            "[SYSTEM DIRECTIVE: NO-EDIT GATE]\n\n"
            f"You have used {calls} tool calls this turn without writing a patch.\n"
            "Stop researching. Write the minimal patch now with patch_file or write_file.\n"
            "If you still need upstream/external research, spawn a read-only researcher\n"
            "(sessions_spawn(functional_role='researcher')) and continue the local patch;\n"
            "do not block the edit on that research."
        )

    def _hard_message(self, used: int, limit: int) -> str:
        return (
            "[SYSTEM DIRECTIVE: NO-EDIT GATE — FINAL]\n\n"
            f"{used}/{limit} of the turn budget is gone and no patch exists.\n"
            "Only read_file and patch_file/write_file are allowed now. Write a minimal\n"
            "patch immediately, then run the checks. Terminal, search, web and\n"
            "researcher-spawn calls are paused until a patch is written."
        )

    def _block_message(self, used: int, limit: int, tool_name: str) -> str:
        return (
            f"Edit gate: no patch after {used}/{limit} calls. "
            f"Write a minimal patch with patch_file/write_file before using {tool_name} "
            "again. read_file and the write tools remain available."
        )

    def _precheck(self, request: ToolCallRequest) -> ToolMessage | None:
        session_id = require_session_id(request.state, "NoEditGate: session_id is required")
        if not _active(session_id):
            return None
        tool_name = str(request.tool_call.get("name", "unknown"))
        args = request.tool_call.get("args")
        gate = self._get(session_id)
        if self._is_write_path(tool_name, args):
            gate.writes += 1
            gate.warned = False
            gate.hard = False
            self._save(session_id, gate)
            return None
        if gate.hard and tool_name not in _ALLOWED_AFTER_HARD:
            used = int(
                state_register_mem.get_state(session_id, StateKey.ITERATION_BUDGET_USED, 0) or 0
            )
            limit = int(
                state_register_mem.get_state(session_id, StateKey.ITERATION_BUDGET, 90) or 90
            )
            return ToolMessage(
                content=self._block_message(used, limit, tool_name),
                tool_call_id=request.tool_call.get("id", ""),
                name=tool_name,
                status="error",
            )
        gate.calls += 1
        self._save(session_id, gate)
        return None

    async def abefore_model(self, state: AgentState, runtime: Any = None) -> dict[str, Any] | None:
        """Warn/hard-inject before the next model call, once per stage."""
        try:
            session_id = self._session_id(state)
            if not _active(session_id):
                return None
            gate = self._get(session_id)
            if gate.writes:
                return None
            used = int(
                state_register_mem.get_state(session_id, StateKey.ITERATION_BUDGET_USED, 0) or 0
            )
            limit = int(
                state_register_mem.get_state(session_id, StateKey.ITERATION_BUDGET, 90) or 90
            )
            if limit > 0 and used >= int(limit * _HARD_RATIO) and not gate.hard:
                gate.hard = True
                self._save(session_id, gate)
                logger.warning("NoEditGate HARD session={} used={}/{}", session_id, used, limit)
                return {"messages": [self._directive(self._hard_message(used, limit))]}
            if gate.calls >= _WARN_AFTER_CALLS and not gate.warned:
                gate.warned = True
                self._save(session_id, gate)
                logger.warning("NoEditGate WARN session={} calls={}", session_id, gate.calls)
                return {"messages": [self._directive(self._warn_message(gate.calls))]}
        except Exception:  # noqa: BLE001 — the gate must never break an eval turn
            logger.exception("NoEditGate: failed; continuing")
        return None

    def before_model(self, state: AgentState, runtime: Any = None) -> dict[str, Any] | None:
        """Sync best-effort path; the streaming main agent uses ``abefore_model``."""
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(self.abefore_model(state, runtime))
        return None

    def wrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], ToolMessage],
    ) -> ToolMessage:
        blocked = self._precheck(request)
        if blocked is not None:
            return blocked
        return handler(request)

    async def awrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], Awaitable[ToolMessage]],
    ) -> ToolMessage:
        blocked = self._precheck(request)
        if blocked is not None:
            return blocked
        return await handler(request)
