"""Delegate-only gate (eval-only, SWE arm).

The main agent's direct exploration and write tools are removed from its model
face and refused at execution. It must delegate research/implementation to
spawned subagents. The child sessions have different ids, so they keep their
full tool surface. Active only with ``SHERRY_EVAL_DELEGATE_ONLY=1`` and an
``evals-swe-`` session id; the product chain never sees it.
"""

from __future__ import annotations

import os
from typing import Any

from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import ToolMessage
from langgraph.prebuilt.tool_node import ToolCallRequest
from loguru import logger

__all__ = ["BLOCKED_TOOL_NAMES", "DelegateOnlyGate"]

#: Direct exploration/write tools the delegated main agent may not use.
BLOCKED_TOOL_NAMES = frozenset(
    {
        "read_file",
        "search_files",
        "message_search",
        "web_search",
        "web_fetch",
        "browser",
        "browser_evaluate",
        "terminal",
        "terminal_bg",
        "python_repl",
        "patch_file",
        "write_file",
        "skill_manage",
    }
)

_REPLY = (
    "Delegate-only eval: [{tool}] is disabled for the main agent. "
    "Spawn a subagent with `sessions_spawn` instead "
    "(functional_role='researcher' for read-only research, "
    "functional_role='executor' for implementation/test work), then poll "
    "`subagents_list` / `sessions_messages` until it is done."
)


def _active(session_id: str) -> bool:
    return os.getenv("SHERRY_EVAL_DELEGATE_ONLY") == "1" and session_id.startswith("evals-swe-")


def _name_of(tool: Any) -> str | None:
    name = getattr(tool, "name", None)
    if name:
        return str(name)
    if isinstance(tool, dict):
        return str(tool.get("name") or "") or None
    return None


class DelegateOnlyGate(AgentMiddleware):
    """Remove the main agent's direct read/search/write tools (eval-only)."""

    @staticmethod
    def _session_id(request: Any) -> str:
        state = getattr(request, "state", None)
        if isinstance(state, dict):
            return str(state.get("session_id") or "")
        return ""

    def _apply(self, request: Any) -> Any:
        if not _active(self._session_id(request)):
            return request
        tools = getattr(request, "tools", None)
        if not isinstance(tools, list):
            return request
        kept = [tool for tool in tools if _name_of(tool) not in BLOCKED_TOOL_NAMES]
        if len(kept) == len(tools):
            return request
        logger.debug(
            "DelegateOnlyGate: main tool face {} -> {} (session {})",
            len(tools),
            len(kept),
            self._session_id(request),
        )
        return request.override(tools=kept)

    def _refusal(self, request: ToolCallRequest) -> ToolMessage | None:
        session_id = str((request.state or {}).get("session_id") or "")
        if not _active(session_id):
            return None
        name = str(request.tool_call.get("name") or "")
        if name not in BLOCKED_TOOL_NAMES:
            return None
        return ToolMessage(
            content=_REPLY.format(tool=name),
            tool_call_id=request.tool_call.get("id", ""),
            name=name,
            status="error",
        )

    def wrap_model_call(self, request: Any, handler: Any) -> Any:
        return handler(self._apply(request))

    async def awrap_model_call(self, request: Any, handler: Any) -> Any:
        return await handler(self._apply(request))

    def wrap_tool_call(self, request: ToolCallRequest, handler: Any) -> Any:
        refusal = self._refusal(request)
        if refusal is not None:
            return refusal
        return handler(request)

    async def awrap_tool_call(self, request: ToolCallRequest, handler: Any) -> Any:
        refusal = self._refusal(request)
        if refusal is not None:
            return refusal
        return await handler(request)
