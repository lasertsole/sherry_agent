"""Plan mode's read-only tool gate.

Plan mode narrows the model's tool face to ``PLAN_MODE["allowed_tools"]`` until
the plan is approved. That gate used to live inside ``ToolSelectionMiddleware``
together with the per-session tool selection; the selection is gone (every
built-in tool is always available), the PLAN gate is not — without it
``enter_plan_mode`` would only be advisory and a model could keep writing files
while a plan awaits approval.

* ``wrap_model_call`` narrows ``request.tools`` tothe whitelist;
* ``wrap_tool_call`` refuses a call that still slips through (a stale checkpoint
  or a hallucinated name).

Fail-open everywhere: an unreadable plan state means "idle", a missing feature
config means "no gating". The two plan tools are always reachable — a mode the
model cannot leave is a trap.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any, override

from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import ToolMessage
from langgraph.prebuilt.tool_node import ToolCallRequest
from loguru import logger

__all__ = ["PlanModeToolGate"]

_PLAN_MODE_REPLY = (
    "Plan mode is active — only read-only tools are available. Explore with "
    "read_file / search_files / message_search, ask the user with `question`, "
    "then submit your plan with exit_plan_mode."
)


def _session_id_of(state: Any) -> str | None:
    """The session behind the current call (``None`` when absent)."""
    if isinstance(state, dict):
        session_id = state.get("session_id")
        if isinstance(session_id, str) and session_id.strip():
            return session_id
    return None


def _plan_active_statuses() -> tuple[str, ...]:
    """The statuses that narrow the tool face (``()`` when the feature is absent)."""
    try:
        from config.features import PLAN_ACTIVE_STATUSES
    except Exception:  # noqa: BLE001 - feature absent => no gating
        return ()
    return tuple(PLAN_ACTIVE_STATUSES)


def _plan_status_of(session_id: str | None) -> str:
    """The session's plan status (``""`` when idle/unreadable — fail-open)."""
    if not session_id:
        return ""
    try:
        from agent.tools.plan_mode.state import read_plan_status

        return read_plan_status(session_id)
    except Exception:  # noqa: BLE001 - never break a turn over the plan state
        logger.debug("PlanModeToolGate: plan status read failed", exc_info=True)
        return ""


def _plan_allowed_tools() -> frozenset[str]:
    """The plan-mode face: the read-only whitelist plus both plan tools."""
    try:
        from config.features import PLAN_MODE

        allowed = {str(name) for name in PLAN_MODE["allowed_tools"]}
    except Exception:  # noqa: BLE001 - fail-open
        return frozenset()
    allowed |= {"enter_plan_mode", "exit_plan_mode"}
    return frozenset(allowed)


def _plan_active(session_id: str | None) -> bool:
    return _plan_status_of(session_id) in _plan_active_statuses()


class PlanModeToolGate(AgentMiddleware):
    """Narrow the model-visible tools to the plan whitelist while a plan is open."""

    @staticmethod
    def _filtered_tools(request: Any) -> Any:
        """The request with only the plan-mode tools (or the request itself)."""
        allowed = _plan_allowed_tools()
        tools = getattr(request, "tools", None)
        if not isinstance(tools, list):
            return request
        kept = [
            tool
            for tool in tools
            if (
                getattr(tool, "name", None)
                or (tool.get("name") if isinstance(tool, dict) else None)
            )
            in allowed
        ]
        if len(kept) == len(tools):
            return request
        logger.debug(
            "PlanModeToolGate: narrowed the tool face {} -> {} for session {}",
            len(tools),
            len(kept),
            _session_id_of(getattr(request, "state", None)),
        )
        return request.override(tools=kept)

    def _apply(self, request: Any) -> Any:
        session_id = _session_id_of(getattr(request, "state", None))
        if _plan_active(session_id):
            return self._filtered_tools(request)
        return request

    @override
    def wrap_model_call(self, request: Any, handler: Any) -> Any:
        return handler(self._apply(request))

    @override
    async def awrap_model_call(self, request: Any, handler: Any) -> Any:
        return await handler(self._apply(request))

    def _rejection(self, request: ToolCallRequest) -> ToolMessage | None:
        """A refusal for a non-whitelisted tool during plan mode, else ``None``."""
        session_id = _session_id_of(getattr(request, "state", None))
        name = str(request.tool_call.get("name") or "")
        if not name or not _plan_active(session_id):
            return None
        if name in _plan_allowed_tools():
            return None
        logger.warning(
            "PlanModeToolGate: refused '{}' during plan mode for session {}",
            name,
            session_id,
        )
        return ToolMessage(
            content=_PLAN_MODE_REPLY,
            tool_call_id=request.tool_call.get("id", ""),
            name=name,
            status="error",
        )

    @override
    def wrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], ToolMessage],
    ) -> ToolMessage:
        refusal = self._rejection(request)
        if refusal is not None:
            return refusal
        return handler(request)

    @override
    async def awrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], Awaitable[ToolMessage]],
    ) -> ToolMessage:
        refusal = self._rejection(request)
        if refusal is not None:
            return refusal
        return await handler(request)
