"""Main-agent delegation-pressure gate (5-read plan nudge, 7-read warn, 12-read escalate).

The counter is cumulative for ONE turn. Three advisory levels, never a block:

- ``plan_nudge_after`` (5 reads) — the agent has read a lot without ever
  planning: remind it to use ``todowrite`` (simple tasks) or
  ``taskflow_flow(action="plan")`` (multi-step tasks). Only tools that are
  actually registered are mentioned.
- ``warn_after`` (7 reads) — delegate the remaining work. When the turn already
  dispatched TaskFlow steps, the message says "wait/dispatch, don't keep
  reading" instead of naming ``sessions_spawn``.
- ``block_after`` (12 reads) — the escalated variant of the same split.

A hand-off resets the counter: an accepted/pending ``sessions_spawn``, or a
TaskFlow dispatch wave (``taskflow_step(action="dispatch")`` /
``taskflow_flow(action="plan")`` auto-dispatch) — both spawn real children, so
the main agent may inspect/verify before a later cycle re-arms. A FAILED spawn
or dispatch does not reset. ``after_agent`` resets everything at the turn
boundary, including the plan-tracking flags: a new turn is a new task and must
be nudged to plan again.

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

#: Tools whose success means structured delegation happened. The grouped
#: TaskFlow surface is ``taskflow_flow`` / ``taskflow_step``; ``taskflow_create``
#: is the legacy alias kept for an older register.
_TASKFLOW_TOOLS = frozenset({"taskflow_flow", "taskflow_step", "taskflow_create"})

#: Successful dispatch texts (see ``taskflow_dispatch._taskflow_dispatch_locked``
#: and ``taskflow_run_task``). An ``Error:`` answer never counts, even when it
#: mentions the partially dispatched ids.
_DISPATCH_MARKERS = (
    "auto-dispatched",
    "steps dispatched",
    "step dispatched",
    "status=accepted",
    "status=pending",
    "subagent spawned",
)


@dataclass
class _PressureState:
    read_calls: int = 0
    warned: bool = False
    escalated: bool = False
    #: The one-time "plan first" nudge already fired this turn.
    plan_nudged: bool = False
    #: ``todowrite`` was called this turn (planning happened).
    has_todowrite: bool = False
    #: A TaskFlow flow was created/planned this turn.
    has_taskflow: bool = False
    #: TaskFlow dispatched at least one step this turn (structured delegation).
    has_taskflow_dispatch: bool = False


def _active(session_id: str) -> bool:
    if not session_id:
        return False
    # Never active for a child, even if a future builder accidentally adds the
    # middleware to the child chain: the gate is a MAIN-agent contract.
    if ":subagent:" in session_id or ":swarm:" in session_id:
        return False
    # Eval opt-in is not tied to a legacy session prefix (no suite uses
    # ``evals-swe-`` any more): the env var alone enables the gate for any
    # non-child session. Child/swarm sessions were excluded above.
    if os.getenv("SHERRY_EVAL_DELEGATION_PRESSURE") == "1":
        return True
    return bool(DELEGATION_PRESSURE["enabled"])


def _result_text(result: Any) -> str:
    """Flatten a tool result's content to text (string or multimodal blocks)."""
    content = getattr(result, "content", result if isinstance(result, str) else "")
    if isinstance(content, list):
        content = " ".join(
            str(part.get("text", "")) if isinstance(part, dict) else str(part) for part in content
        )
    return str(content or "")


class DelegationPressureGate(AfterAgentHooksMixin, BeforeAgentHooksMixin, AgentMiddleware):
    """Nudge a plan at 5 reads, warn at 7, escalate at 12, reset on a hand-off."""

    def __init__(self) -> None:
        super().__init__()
        self._pressure_tools = frozenset(DELEGATION_PRESSURE["pressure_tools"])
        self._excluded_tools = frozenset(DELEGATION_PRESSURE["excluded_tools"])
        #: (has todowrite, has taskflow_*) — built lazily: constructing the
        #: main tool list at import would run every tool builder for nothing.
        self._tool_availability: tuple[bool, bool] | None = None

    @staticmethod
    def _session_id(state: AgentState) -> str:
        return require_session_id(state, "DelegationPressureGate: session_id is required")

    def _get(self, session_id: str) -> _PressureState:
        state = state_register_mem.get_state(session_id, _STATE_KEY, None)
        return state if isinstance(state, _PressureState) else _PressureState()

    def _save(self, session_id: str, state: _PressureState) -> None:
        state_register_mem.set_state(session_id, _STATE_KEY, state)

    def _planning_tools(self) -> tuple[bool, bool]:
        """Whether ``todowrite`` / ``taskflow_*`` are registered (cached once)."""
        if self._tool_availability is None:
            try:
                from agent.tools import build_main_tools

                tools = build_main_tools()
                items = tools if isinstance(tools, list) else [tools]
                names = {str(getattr(tool, "name", "")) for tool in items}
                self._tool_availability = (
                    "todowrite" in names,
                    any(name.startswith("taskflow_") for name in names),
                )
            except Exception:  # noqa: BLE001 - availability is best-effort
                logger.exception("DelegationPressureGate: tool availability probe failed")
                self._tool_availability = (False, False)
        return self._tool_availability

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
            # A fresh state clears read_calls, the warn/escalate latches AND the
            # plan-tracking flags: the next turn is a new task and must be
            # nudged to plan again.
            self._save(session_id, _PressureState())
        except Exception:  # noqa: BLE001 - the turn-end hook must never fail a turn
            logger.exception("DelegationPressureGate: after_agent failed; continuing")

    # ── directives ──────────────────────────────────────────────────────────
    def _directive(self, content: str) -> HumanMessage:
        return HumanMessage(
            content=wrap_reminder("delegation_pressure", content),
            metadata=system_reminder_metadata("delegation_pressure"),
        )

    def _plan_message(self, calls: int) -> str:
        has_todowrite, has_taskflow = self._planning_tools()
        tips: list[str] = []
        if has_todowrite:
            tips.append("- For simple tasks: use `todowrite` to track your steps.")
        if has_taskflow:
            tips.append(
                "- For complex multi-step tasks with dependencies: use "
                '`taskflow_flow(action="plan")` to create a structured DAG that '
                "auto-creates a todo, dispatches steps to subagents, and tracks "
                "completion."
            )
        return (
            f"PLANNING NUDGE: You have made {calls} read/exploration calls "
            "without creating a task plan. Before diving deeper, consider:\n"
            + "\n".join(tips)
            + "\nA plan helps you track progress, delegate effectively, and avoid "
            "missing steps. This is advisory — proceed without a plan if the task "
            "is simple enough."
        )

    def _warn_message(self, calls: int, has_taskflow_dispatch: bool) -> str:
        if has_taskflow_dispatch:
            return (
                f"You have made {calls} read calls while your TaskFlow subagents are "
                "working.\nInstead of reading extensively yourself, consider:\n"
                '- `taskflow_step(action="wait_all")` to wait for dispatched steps to '
                "complete.\n"
                '- `taskflow_step(action="dispatch")` to dispatch more ready steps.\n'
                "Let your subagents do the reading — you focus on coordination."
            )
        return (
            f"You have made {calls} read/exploration calls this turn.\n"
            "Consider delegating the remaining work: call `sessions_spawn` with\n"
            "functional_role='researcher' for read-only research, or 'executor' for\n"
            "implementation/test work. Tools remain available."
        )

    def _escalated_message(self, calls: int, has_taskflow_dispatch: bool) -> str:
        if has_taskflow_dispatch:
            return (
                "[DELEGATION PRESSURE — ESCALATED]\n\n"
                f"You have made {calls} read calls while TaskFlow subagents are "
                "working.\nSTOP reading. Your subagents are doing the work.\n"
                'Call `taskflow_step(action="wait_all")` to wait for results, then '
                '`taskflow_step(action="resume")` to inject them.\n'
                "Do not continue reading on your own."
            )
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
        text = _result_text(result).lower()
        return any(
            marker in text
            for marker in (
                "subagent spawned",
                "status=accepted",
                "status=pending",
            )
        )

    @staticmethod
    def _taskflow_dispatched(result: Any) -> bool:
        """True when a TaskFlow call really spawned child work.

        A dispatch answer that starts with ``Error:`` never counts (the plan's
        kill criterion: a failed batch must not release the pressure).
        """
        status = str(getattr(result, "status", "") or "").lower()
        if status in {"error", "failed"}:
            return False
        text = _result_text(result)
        if text.lstrip().lower().startswith("error"):
            return False
        lowered = text.lower()
        return any(marker in lowered for marker in _DISPATCH_MARKERS)

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
        state = self._get(session_id)

        if name == "todowrite":
            if not state.has_todowrite:
                state.has_todowrite = True
                self._save(session_id, state)
        elif name in _TASKFLOW_TOOLS:
            if not state.has_taskflow:
                state.has_taskflow = True
            dispatched = self._taskflow_dispatched(result)
            if dispatched:
                state.has_taskflow_dispatch = True
            self._save(session_id, state)
            if dispatched:
                # A TaskFlow dispatch wave is a hand-off exactly like an
                # accepted spawn: restart the read count so the main agent can
                # inspect/verify. ``plan_nudged`` stays (the plan exists).
                state.read_calls = 0
                state.warned = False
                state.escalated = False
                self._save(session_id, state)
                logger.info(
                    "DelegationPressureGate: TaskFlow dispatch reset the pressure count session={}",
                    session_id,
                )
                return

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
        """Inject the one-time plan nudge / warning / escalated system reminder."""
        try:
            session_id = self._session_id(state)
            if not _active(session_id):
                return None
            gate = self._get(session_id)
            has_todowrite_tool, has_taskflow_tool = self._planning_tools()

            # 1. Plan nudge (lowest threshold): reads without any planning in
            #    this turn, and at least one planning tool is actually available.
            if (
                not gate.plan_nudged
                and gate.read_calls >= int(DELEGATION_PRESSURE["plan_nudge_after"])
                and not gate.has_todowrite
                and not gate.has_taskflow
                and (has_todowrite_tool or has_taskflow_tool)
            ):
                gate.plan_nudged = True
                self._save(session_id, gate)
                return {"messages": [self._directive(self._plan_message(gate.read_calls))]}

            # 2. Escalated warn (highest threshold, checked first as before).
            if gate.read_calls >= DELEGATION_PRESSURE["block_after"] and not gate.escalated:
                gate.escalated = True
                self._save(session_id, gate)
                return {
                    "messages": [
                        self._directive(
                            self._escalated_message(gate.read_calls, gate.has_taskflow_dispatch)
                        )
                    ]
                }

            # 3. Ordinary warn.
            if (
                not gate.escalated
                and gate.read_calls >= DELEGATION_PRESSURE["warn_after"]
                and not gate.warned
            ):
                gate.warned = True
                self._save(session_id, gate)
                return {
                    "messages": [
                        self._directive(
                            self._warn_message(gate.read_calls, gate.has_taskflow_dispatch)
                        )
                    ]
                }
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
