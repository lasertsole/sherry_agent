"""ToolSelectionMiddleware — the session's tool set, applied per model call.

The 预设-工具 tab stores a per-session list of enabled tool names
(``AGENT_CONFIG["tools"]``, written by ``PUT /sessions/agent_config``). The
compiled graph is shared by every session and its ``ToolNode`` is built from the
full process-wide tool list, so the selection is applied WHERE IT MATTERS —
at call time:

* ``wrap_model_call`` narrows ``request.tools`` before the model is bound
  (langchain re-binds the tool schema from ``request.tools`` on every call), so
  a disabled tool is not even offered to the model;
* ``wrap_tool_call`` refuses to EXECUTE one that still gets called — a stale
  checkpoint can hold a call that predates the switch, and a model can
  hallucinate a name; the ToolNode would otherwise run it happily.

Unset config (or ``tools: null``) = everything on, which keeps a session without
a selection byte-for-byte on the old behaviour. Fail-open: an unreadable
register or a malformed payload logs and passes the request through untouched.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any, override

from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import ToolMessage
from langgraph.prebuilt.tool_node import ToolCallRequest
from loguru import logger

__all__ = ["ToolSelectionMiddleware", "enabled_tool_names"]

_DISABLED_REPLY = (
    "Tool [{tool}] is disabled for this session by its agent configuration "
    "(预设 → 工具). Continue without it, or ask the user to enable it again."
)


def _session_id_of(state: Any) -> str | None:
    """The session behind the current call (``None`` when absent)."""
    if isinstance(state, dict):
        session_id = state.get("session_id")
        if isinstance(session_id, str) and session_id.strip():
            return session_id
    return None


def enabled_tool_names(session_id: str | None) -> frozenset[str] | None:
    """The session's enabled tool names, or ``None`` when unset (= all enabled).

    REQUIRED tools are unioned back in (``agent.tools.catalog.REQUIRED_TOOLS``):
    the service rejects a payload that omits one and the catalogue marks it
    locked, but a register value written before a tool became required must not
    be able to strip it either — this is the single guard for that path.

    Mem-register only: this runs on the model hot path, so no blocking I/O, and
    any failure degrades to ``None`` (all tools) rather than an empty set — a
    broken config must not leave a session tool-less.
    """
    if not session_id:
        return None
    try:
        from agent.tools.catalog import REQUIRED_TOOLS
        from runtime import StateKey, state_register_mem

        raw = state_register_mem.get_state(session_id, StateKey.AGENT_CONFIG, None)
        if not isinstance(raw, dict):
            return None
        names = raw.get("tools")
        if not isinstance(names, list):
            return None
        stored = frozenset(str(name) for name in names if isinstance(name, str) and name)
        return stored | REQUIRED_TOOLS
    except Exception:  # noqa: BLE001 - never break a turn over the config read
        logger.exception("ToolSelectionMiddleware: config read failed; offering every tool")
        return None


class ToolSelectionMiddleware(AgentMiddleware):
    """Narrow the model-visible tools and refuse disabled ones at execution."""

    @staticmethod
    def _filtered_tools(request: Any, enabled: frozenset[str]) -> Any:
        """The request with only the enabled tools (or the request itself)."""
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
            in enabled
        ]
        if len(kept) == len(tools):
            return request
        dropped = [
            (getattr(tool, "name", None) or (tool.get("name") if isinstance(tool, dict) else None))
            for tool in tools
            if tool not in kept
        ]
        logger.debug(
            "ToolSelection: narrowed the tool face {} -> {} for session {} (disabled: {})",
            len(tools),
            len(kept),
            _session_id_of(getattr(request, "state", None)),
            ", ".join(str(name) for name in dropped[:10]) or "-",
        )
        return request.override(tools=kept)

    def _apply(self, request: Any) -> Any:
        enabled = enabled_tool_names(_session_id_of(getattr(request, "state", None)))
        if enabled is None:
            return request
        return self._filtered_tools(request, enabled)

    @override
    def wrap_model_call(self, request: Any, handler: Any) -> Any:
        return handler(self._apply(request))

    @override
    async def awrap_model_call(self, request: Any, handler: Any) -> Any:
        return await handler(self._apply(request))

    def _rejection(self, request: ToolCallRequest) -> ToolMessage | None:
        """A refusal for a disabled tool, or ``None`` to run it."""
        enabled = enabled_tool_names(_session_id_of(getattr(request, "state", None)))
        name = str(request.tool_call.get("name") or "")
        if enabled is None or not name or name in enabled:
            return None
        logger.warning(
            "ToolSelectionMiddleware: refused disabled tool '{}' for session {}",
            name,
            _session_id_of(getattr(request, "state", None)),
        )
        return ToolMessage(
            content=_DISABLED_REPLY.format(tool=name),
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
