"""``wrap_tool_call``: inject the tool-result queue's output into a ToolMessage."""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from typing import Any

from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import ToolMessage
from langgraph.prebuilt.tool_node import ToolCallRequest
from loguru import logger

from .core import resolve_injections

__all__ = ["ToolResultEnrichmentMiddleware"]


class ToolResultEnrichmentMiddleware(AgentMiddleware):
    """Outermost ``wrap_tool_call``: append injector output to successful results.

    Registered BEFORE ``ToolGuardrails`` (outermost), so it sees the final
    result after every other middleware has processed it. Error results are
    passed through untouched — a blocked/halted tool call receives no hints.

    **JSON-aware injection**: a JSON object result (``read_file`` and friends)
    gets an ``"advisory"`` field, keeping ``json.loads`` valid for the
    downstream eviction / truncation pipeline. Plain text gets a suffix. The
    mode is auto-detected; tools declare nothing.
    """

    def _enrich(self, request: ToolCallRequest, result: Any) -> Any:
        if not isinstance(result, ToolMessage):
            return result
        if str(getattr(result, "status", "") or "").lower() == "error":
            return result

        state = getattr(request, "state", None) or {}
        session_id = str(state.get("session_id") or "") if isinstance(state, dict) else ""
        if not session_id:
            return result

        call = request.tool_call or {}
        tool = getattr(request, "tool", None)
        tool_name = str(call.get("name") or getattr(tool, "name", "") or "")
        tool_args = call.get("args") or {}
        try:
            injections = resolve_injections(tool_name, tool_args, result, session_id)
        except Exception:  # noqa: BLE001 - the tool result must survive a bad injector
            logger.exception("ToolResultEnrichmentMiddleware: resolve failed; returning raw result")
            return result
        if not injections:
            return result

        suffix = "\n\n".join(injections)
        content = result.content if isinstance(result.content, str) else str(result.content)
        try:
            payload = json.loads(content)
            if isinstance(payload, dict):
                existing = payload.get("advisory")
                payload["advisory"] = (
                    f"{existing}\n\n{suffix}"
                    if isinstance(existing, str) and existing.strip()
                    else suffix
                )
                new_content = json.dumps(payload, ensure_ascii=False, default=str)
            else:
                new_content = f"{content}\n\n{suffix}"
        except (json.JSONDecodeError, TypeError):
            new_content = f"{content}\n\n{suffix}"

        try:
            return result.model_copy(update={"content": new_content})
        except Exception:  # noqa: BLE001 - an old message shape still gets the text
            return ToolMessage(
                content=new_content,
                tool_call_id=result.tool_call_id,
                name=result.name,
                status=result.status,
            )

    async def awrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], Awaitable[Any]],
    ) -> Any:
        result = await handler(request)
        return self._enrich(request, result)

    def wrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], Any],
    ) -> Any:
        result = handler(request)
        return self._enrich(request, result)
