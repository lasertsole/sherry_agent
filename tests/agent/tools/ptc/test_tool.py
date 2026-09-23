"""Unit tests for the ``execute_code`` LangChain tool."""

from __future__ import annotations

import asyncio

import pytest

from agent.tools.file_tools import build_read_file_tool
from agent.tools.ptc import build_ptc_tool
from agent.tools.ptc.tool import ExecuteCodeTool
from agent.tools.terminal import build_terminal_tool
from config.features import PTC

pytestmark = [pytest.mark.unit]


class _NamedTool:
    def __init__(self, name: str) -> None:
        self.name = name
        self.tool_call_schema = None
        self.metadata: dict = {}


def _build() -> ExecuteCodeTool:
    return build_ptc_tool(
        available_tools=[
            build_read_file_tool(),
            build_terminal_tool(),
            _NamedTool("memory"),
            _NamedTool("sessions_spawn"),
            _NamedTool("execute_code"),
            _NamedTool("web_search"),
        ],
        session_id="agent:executor:subagent:abc",
    )


def test_identity_metadata_and_scope() -> None:
    tool = _build()
    assert tool.name == "execute_code"
    assert tool.metadata == {"idempotent": False, "scope": "subagent_only"}


def test_args_schema_exposes_only_code() -> None:
    schema = ExecuteCodeTool.model_fields["args_schema"].default
    assert set(schema.model_fields) == {"code"}


def test_tool_map_is_intersection_and_excludes_self_and_dangerous() -> None:
    tool = _build()
    assert set(tool._tools_map) == {"read_file", "terminal", "web_search"}
    assert "execute_code" not in tool._tools_map
    assert "memory" not in tool._tools_map
    assert "sessions_spawn" not in tool._tools_map


def test_description_lists_only_available_tools() -> None:
    tool = _build()
    assert "read_file(file_path" in tool.description
    assert "terminal(commands" in tool.description
    assert "memory" not in tool.description
    assert "sessions_spawn" not in tool.description
    assert "execute_code" not in tool.description
    assert "max 50 tool calls" in tool.description


def test_session_id_is_bound_not_user_supplied() -> None:
    tool = _build()
    assert tool._session_id == "agent:executor:subagent:abc"
    assert "session_id" not in tool.description


def test_execute_code_is_not_in_its_own_whitelist() -> None:
    assert "execute_code" not in PTC["ptc_allowed_tools"]


def test_arun_delegates_to_run_ptc_with_bound_session(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict = {}

    async def _fake_run_ptc(code, tools_map, session_id, config, **kwargs):
        captured.update(code=code, tools_map=tools_map, session_id=session_id, config=config)
        return '{"status": "ok"}'

    monkeypatch.setattr("agent.tools.ptc.tool.run_ptc", _fake_run_ptc)
    tool = _build()
    result = asyncio.run(tool._arun("print(1)"))
    assert result == '{"status": "ok"}'
    assert captured["code"] == "print(1)"
    assert captured["session_id"] == "agent:executor:subagent:abc"
    assert captured["config"] is PTC
