"""Behavior tests for ToolResultEnrichmentMiddleware."""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import pytest
from langchain_core.messages import ToolMessage

from agent.middlewares.tool_result_enrichment import core as trc
from agent.middlewares.tool_result_enrichment.core import ToolResultInjector
from agent.middlewares.tool_result_enrichment.middleware import ToolResultEnrichmentMiddleware


pytestmark = [pytest.mark.unit]


@pytest.fixture(autouse=True)
def _isolated_registry(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(trc, "_INJECTORS", {})
    monkeypatch.setattr(trc, "_CHANGE_STATE", {})


def _request(session: str = "sess-1", tool: str = "read_file") -> Any:
    return SimpleNamespace(
        state={"session_id": session},
        tool_call={"name": tool, "args": {}, "id": "call-1"},
        tool=SimpleNamespace(name=tool),
    )


def _register_hint(text: str = "HINT") -> None:
    trc.register(
        ToolResultInjector(
            "hint",
            lambda *_args: text,
        )
    )


async def _ok(_request: Any) -> ToolMessage:
    return ToolMessage(content="plain", tool_call_id="call-1", name="read_file")


@pytest.mark.asyncio
async def test_plain_text_gets_a_suffix():
    _register_hint()
    mw = ToolResultEnrichmentMiddleware()

    result = await mw.awrap_tool_call(_request(), _ok)

    assert result.content == "plain\n\nHINT"
    assert result.tool_call_id == "call-1"


@pytest.mark.asyncio
async def test_json_object_gets_an_advisory_field():
    _register_hint()

    async def _json(_request: Any) -> ToolMessage:
        return ToolMessage(
            content=json.dumps({"content": "x", "truncated": False}),
            tool_call_id="call-1",
            name="read_file",
        )

    result = await ToolResultEnrichmentMiddleware().awrap_tool_call(_request(), _json)

    payload = json.loads(str(result.content))
    assert payload["content"] == "x"
    assert payload["advisory"] == "HINT"


@pytest.mark.asyncio
async def test_an_existing_advisory_is_appended_not_replaced():
    _register_hint()

    async def _json(_request: Any) -> ToolMessage:
        return ToolMessage(
            content=json.dumps({"content": "x", "advisory": "OLD"}),
            tool_call_id="call-1",
            name="read_file",
        )

    result = await ToolResultEnrichmentMiddleware().awrap_tool_call(_request(), _json)

    assert json.loads(str(result.content))["advisory"] == "OLD\n\nHINT"


@pytest.mark.asyncio
async def test_error_results_are_untouched():
    _register_hint()

    async def _error(_request: Any) -> ToolMessage:
        return ToolMessage(content="boom", tool_call_id="call-1", name="read_file", status="error")

    result = await ToolResultEnrichmentMiddleware().awrap_tool_call(_request(), _error)

    assert result.content == "boom"


@pytest.mark.asyncio
async def test_non_tool_messages_are_untouched():
    _register_hint()

    async def _other(_request: Any) -> str:
        return "not a message"

    result = await ToolResultEnrichmentMiddleware().awrap_tool_call(_request(), _other)

    assert result == "not a message"


@pytest.mark.asyncio
async def test_empty_injections_are_a_noop():
    mw = ToolResultEnrichmentMiddleware()

    result = await mw.awrap_tool_call(_request(), _ok)

    assert result.content == "plain"


@pytest.mark.asyncio
async def test_missing_session_is_a_noop():
    _register_hint()

    result = await ToolResultEnrichmentMiddleware().awrap_tool_call(_request(session=""), _ok)

    assert result.content == "plain"


def test_sync_path_enriches_too():
    _register_hint()

    def _sync(_request: Any) -> ToolMessage:
        return ToolMessage(content="plain", tool_call_id="call-1", name="read_file")

    result = ToolResultEnrichmentMiddleware().wrap_tool_call(_request(), _sync)

    assert result.content == "plain\n\nHINT"
