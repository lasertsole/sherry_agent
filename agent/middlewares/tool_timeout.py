"""Middleware that applies a timeout to each individual tool call.

When a tool call exceeds the configured timeout, the middleware intercepts
it and returns an error ``ToolMessage`` to the model instead of letting it
hang indefinitely.  The timeout value is read from ``TOOL_CALL_TIMEOUT_MINUTES``
in the environment configuration (dotenv)."""

import os
import asyncio
from loguru import logger
from config import ENV_PATH
from dotenv import load_dotenv
from langgraph.types import Command
from typing_extensions import override
from typing import Any, Callable, Awaitable
from langchain_core.messages import ToolMessage
from langchain.agents.middleware import AgentMiddleware
from langgraph.prebuilt.tool_node import ToolCallRequest

load_dotenv(ENV_PATH, override=True)

_TIMEOUT_MINUTES_STR: str | None = os.getenv("TOOL_CALL_TIMEOUT_MINUTES")

if _TIMEOUT_MINUTES_STR:
    try:
        TOOL_CALL_TIMEOUT_SECONDS: float = float(_TIMEOUT_MINUTES_STR) * 60.0
        if TOOL_CALL_TIMEOUT_SECONDS <= 0.0:
            raise ValueError(
                "TOOL_CALL_TIMEOUT_MINUTES must be a positive number"
            )
    except ValueError as e:
        logger.error(
            "Invalid value for TOOL_CALL_TIMEOUT_MINUTES: %s, error: %s",
            _TIMEOUT_MINUTES_STR,
            e,
        )
        raise
else:
    TOOL_CALL_TIMEOUT_SECONDS = 0.0  # disabled


class ToolTimeout(AgentMiddleware):
    """Wrap every tool invocation with ``asyncio.wait_for`` so that a tool
    that hangs for longer than ``TOOL_CALL_TIMEOUT_MINUTES`` is cancelled and
    an error ``ToolMessage`` is returned to the model."""

    def __init__(self, timeout_seconds: float | None = None):
        super().__init__()
        self._timeout_seconds: float = (
            timeout_seconds if timeout_seconds is not None else TOOL_CALL_TIMEOUT_SECONDS
        )

    # ------------------------------------------------------------------
    # Timeout implementation (shared by sync + async)
    # ------------------------------------------------------------------
    def _wrap_tool_call_impl(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], ToolMessage],
    ) -> ToolMessage:
        """Synchronous variant: no asyncio.wait_for, just pass through."""
        return handler(request)

    # ------------------------------------------------------------------
    # Sync wrap_tool_call
    # ------------------------------------------------------------------
    @override
    def wrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], ToolMessage],
    ) -> ToolMessage:
        return self._wrap_tool_call_impl(request, handler)

    # ------------------------------------------------------------------
    # Async wrap_tool_call
    # ------------------------------------------------------------------
    @override
    async def awrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], Awaitable[ToolMessage | Command[Any]]],
    ) -> ToolMessage | Command[Any]:
        timeout = self._timeout_seconds
        if timeout <= 0.0:
            return await handler(request)

        tool_name: str = request.tool_call.get("name", "unknown")
        tool_call_id: str = request.tool_call.get("id", "")

        try:
            return await asyncio.wait_for(
                handler(request),
                timeout=timeout,
            )
        except asyncio.TimeoutError:
            logger.warning(
                "Tool [%s] timed out after %.1f seconds. "
                "Returning error to model.",
                tool_name,
                timeout,
            )
            return ToolMessage(
                content=(
                    f"Tool [{tool_name}] did not return within "
                    f"{timeout:.0f} seconds "
                    f"and timed out. "
                    "Please try a different approach or retry later."
                ),
                tool_call_id=tool_call_id,
                name=tool_name,
                status="error",
            )
