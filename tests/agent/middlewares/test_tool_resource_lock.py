"""Resource-lock middleware: system is process-wide, interaction is per session."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from langchain_core.messages import ToolMessage

from agent.middlewares.tool_resource_lock import ToolResourceLockMiddleware
from config.features import TOOL_PARALLEL

pytestmark = [pytest.mark.unit, pytest.mark.timeout(30)]


def _request(scope: str, session_id: str = "s1") -> SimpleNamespace:
    return SimpleNamespace(
        tool=SimpleNamespace(metadata={"side_effect_scope": scope}),
        state={"session_id": session_id},
    )


def _message() -> ToolMessage:
    return ToolMessage(content="ok", tool_call_id="c1")


def test_lock_key_only_covers_the_two_configured_scopes(monkeypatch) -> None:
    monkeypatch.setitem(TOOL_PARALLEL, "system_lock", True)
    monkeypatch.setitem(TOOL_PARALLEL, "interaction_lock", True)
    middleware = ToolResourceLockMiddleware()

    assert middleware._lock_key(_request("system")) == "system"
    assert middleware._lock_key(_request("user_interaction", "s1")) == "interaction:s1"
    assert middleware._lock_key(_request("user_interaction", "s2")) == "interaction:s2"
    assert middleware._lock_key(_request("workspace")) is None
    assert middleware._lock_key(_request("session")) is None
    assert middleware._lock_key(_request("network")) is None


def test_disabled_switches_pass_through(monkeypatch) -> None:
    monkeypatch.setitem(TOOL_PARALLEL, "system_lock", False)
    monkeypatch.setitem(TOOL_PARALLEL, "interaction_lock", False)
    middleware = ToolResourceLockMiddleware()

    assert middleware._lock_key(_request("system")) is None
    assert middleware._lock_key(_request("user_interaction")) is None


def test_system_calls_are_serialized_across_sessions(monkeypatch) -> None:
    monkeypatch.setitem(TOOL_PARALLEL, "system_lock", True)
    middleware = ToolResourceLockMiddleware()

    async def scenario() -> list[str]:
        order: list[str] = []
        first_started = asyncio.Event()
        release_first = asyncio.Event()

        async def first_handler(request):
            order.append("first:start")
            first_started.set()
            await release_first.wait()
            order.append("first:end")
            return _message()

        async def second_handler(request):
            order.append("second:start")
            return _message()

        first = asyncio.create_task(
            middleware.awrap_tool_call(_request("system", "s1"), first_handler)
        )
        await first_started.wait()
        second = asyncio.create_task(
            middleware.awrap_tool_call(_request("system", "s2"), second_handler)
        )
        await asyncio.sleep(0.05)
        assert order == ["first:start"]
        release_first.set()
        await asyncio.gather(first, second)
        return order

    assert asyncio.run(scenario()) == ["first:start", "first:end", "second:start"]


def test_interaction_calls_serialize_only_within_one_session(monkeypatch) -> None:
    monkeypatch.setitem(TOOL_PARALLEL, "interaction_lock", True)
    middleware = ToolResourceLockMiddleware()

    async def scenario() -> list[str]:
        order: list[str] = []
        first_started = asyncio.Event()
        release_first = asyncio.Event()

        async def first_handler(request):
            order.append("first:start")
            first_started.set()
            await release_first.wait()
            order.append("first:end")
            return _message()

        async def other(request):
            order.append("other:start")
            return _message()

        first = asyncio.create_task(
            middleware.awrap_tool_call(_request("user_interaction", "s1"), first_handler)
        )
        await first_started.wait()
        # A different session does not wait.
        await middleware.awrap_tool_call(_request("user_interaction", "s2"), other)
        same_session = asyncio.create_task(
            middleware.awrap_tool_call(_request("user_interaction", "s1"), other)
        )
        await asyncio.sleep(0.05)
        assert "other:start" in order
        release_first.set()
        await asyncio.gather(first, same_session)
        return order

    assert asyncio.run(scenario()).count("other:start") == 2


def test_workspace_scope_is_not_locked(monkeypatch) -> None:
    monkeypatch.setitem(TOOL_PARALLEL, "system_lock", True)
    middleware = ToolResourceLockMiddleware()
    assert middleware._lock_key(_request("workspace")) is None
