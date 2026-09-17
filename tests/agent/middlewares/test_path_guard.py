"""Unit tests for the PathGuard middleware.

The fake handler stands in for a tool that does NOT call
``resolve_project_path()`` itself — proving the middleware rejects traversal
and hard-denied paths even when the tool's own gates are missing. External
paths are intentionally passed through to the tool's ``resolve_external_path()``
HITL flow.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
from langchain_core.messages import ToolMessage
from langgraph.prebuilt.tool_node import ToolCallRequest

from agent.middlewares.path_guard import PathGuard
from config.path import ROOT_DIR

pytestmark = [pytest.mark.unit, pytest.mark.timeout(60)]


def _request(path: str, *, key: str = "file_path", tool: str = "fake_tool") -> ToolCallRequest:
    return ToolCallRequest(
        tool_call={"name": tool, "args": {key: path}, "id": "call-1"},
        tool=None,
        state={"session_id": "s1"},
        runtime=None,
    )


def _ok_handler(record: list[ToolCallRequest]):
    def _handler(request: ToolCallRequest) -> ToolMessage:
        record.append(request)
        return ToolMessage(content="ran", tool_call_id="call-1", name="fake_tool")

    return _handler


class TestRejection:
    def test_rejects_absolute_system_credential_path(self):
        record: list[ToolCallRequest] = []
        result = PathGuard().wrap_tool_call(_request("/etc/passwd"), _ok_handler(record))
        assert isinstance(result, ToolMessage)
        assert result.status == "error"
        assert "PathGuard" in str(result.content)
        assert record == [], "rejected call must not reach the tool handler"

    def test_rejects_traversal_component(self):
        record: list[ToolCallRequest] = []
        result = PathGuard().wrap_tool_call(_request("../../etc/passwd"), _ok_handler(record))
        assert isinstance(result, ToolMessage) and result.status == "error"
        assert record == []

    def test_rejects_url_encoded_traversal(self):
        record: list[ToolCallRequest] = []
        result = PathGuard().wrap_tool_call(
            _request("%2e%2e/%2e%2e/etc/passwd"), _ok_handler(record)
        )
        assert isinstance(result, ToolMessage) and result.status == "error"
        assert record == []

    def test_rejects_yolo_denied_home_path(self):
        record: list[ToolCallRequest] = []
        result = PathGuard().wrap_tool_call(
            _request(str(Path.home() / ".ssh" / "id_rsa")), _ok_handler(record)
        )
        assert isinstance(result, ToolMessage) and result.status == "error"
        assert record == []

    def test_rejection_preserves_tool_call_id_and_name(self):
        record: list[ToolCallRequest] = []
        result = PathGuard().wrap_tool_call(_request("/etc/passwd"), _ok_handler(record))
        assert result.tool_call_id == "call-1"
        assert result.name == "fake_tool"

    @pytest.mark.parametrize("key", ["path", "directory", "dir"])
    def test_all_path_argument_names_are_screened(self, key: str):
        record: list[ToolCallRequest] = []
        result = PathGuard().wrap_tool_call(_request("../outside", key=key), _ok_handler(record))
        assert isinstance(result, ToolMessage) and result.status == "error"
        assert record == []


class TestPassThrough:
    def test_project_relative_path_reaches_handler(self):
        record: list[ToolCallRequest] = []
        result = PathGuard().wrap_tool_call(_request("src/main.py"), _ok_handler(record))
        assert result.content == "ran"
        assert len(record) == 1

    def test_project_absolute_path_reaches_handler(self):
        record: list[ToolCallRequest] = []
        result = PathGuard().wrap_tool_call(
            _request(str(ROOT_DIR / "src" / "main.py")), _ok_handler(record)
        )
        assert result.content == "ran"
        assert len(record) == 1

    def test_external_path_is_left_to_the_tool(self, tmp_path: Path):
        record: list[ToolCallRequest] = []
        external = str(tmp_path.resolve() / "notes.txt")
        result = PathGuard().wrap_tool_call(_request(external), _ok_handler(record))
        assert result.content == "ran", "external paths go through the tool's own approval flow"
        assert len(record) == 1

    def test_tilde_home_path_is_left_to_the_tool(self, tmp_path: Path, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setenv("USERPROFILE", str(tmp_path))
        record: list[ToolCallRequest] = []
        result = PathGuard().wrap_tool_call(_request("~/notes.txt"), _ok_handler(record))
        assert result.content == "ran"
        assert len(record) == 1

    def test_url_shaped_value_is_not_treated_as_a_path(self):
        record: list[ToolCallRequest] = []
        result = PathGuard().wrap_tool_call(
            _request("https://example.com/a/../b"), _ok_handler(record)
        )
        assert result.content == "ran"
        assert len(record) == 1

    def test_non_string_and_missing_args_pass(self):
        record: list[ToolCallRequest] = []
        request = ToolCallRequest(
            tool_call={"name": "fake_tool", "args": {"file_path": 42}, "id": "call-2"},
            tool=None,
            state={"session_id": "s1"},
            runtime=None,
        )
        result = PathGuard().wrap_tool_call(request, _ok_handler(record))
        assert result.content == "ran"
        assert len(record) == 1

    def test_missing_object_never_touches_filesystem(self):
        record: list[ToolCallRequest] = []
        request = _request(str(ROOT_DIR / "no-such-file-xyz.txt"))
        result = PathGuard().wrap_tool_call(request, _ok_handler(record))
        assert result.content == "ran"
        assert len(record) == 1


class TestAsync:
    def test_awrap_rejects_without_calling_handler(self):
        record: list[ToolCallRequest] = []

        async def _handler(request: ToolCallRequest) -> ToolMessage:
            record.append(request)
            return ToolMessage(content="ran", tool_call_id="call-1", name="fake_tool")

        result = asyncio.run(PathGuard().awrap_tool_call(_request("/etc/passwd"), _handler))
        assert isinstance(result, ToolMessage) and result.status == "error"
        assert record == []

    def test_awrap_passes_normal_paths(self):
        record: list[ToolCallRequest] = []

        async def _handler(request: ToolCallRequest) -> ToolMessage:
            record.append(request)
            return ToolMessage(content="ran", tool_call_id="call-1", name="fake_tool")

        result = asyncio.run(PathGuard().awrap_tool_call(_request("src/app.py"), _handler))
        assert result.content == "ran"
        assert len(record) == 1


@pytest.mark.asyncio
async def test_built_agent_registers_path_guard_after_tool_call_normalize(
    patched_agent_core: tuple[Any, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    from agent import core as agent_core
    from agent.middlewares import ToolCallNormalize, ToolGuardrails

    # The shared fixture replaces middleware classes with placeholders; the two
    # neighbours this test positions PathGuard against must be the real ones.
    monkeypatch.setattr(agent_core, "ToolCallNormalize", ToolCallNormalize)
    monkeypatch.setattr(agent_core, "ToolGuardrails", ToolGuardrails)
    captured: dict[str, Any] = {}

    def _capture(**kwargs: Any) -> Any:
        captured["middleware"] = kwargs["middleware"]
        return patched_agent_core[1]

    monkeypatch.setattr(agent_core, "create_agent", _capture)
    await agent_core.built_agent()

    names = [type(mw).__name__ for mw in captured["middleware"]]
    assert "PathGuard" in names, names
    assert names.index("PathGuard") == names.index("ToolCallNormalize") + 1, names
    assert names.index("ToolGuardrails") < names.index("PathGuard"), (
        f"PathGuard must sit inside ToolGuardrails so rejections are evaluated: {names}"
    )
