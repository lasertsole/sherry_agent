"""workflow_replay tool surface."""

from __future__ import annotations

import json

import pytest

from agent.tools import _MAIN_TOOLS_BUILDERS
from agent.tools.ldw import tool as tool_mod
from agent.tools.ldw.tool import WorkflowReplayTool, build_workflow_replay_tool

pytestmark = [pytest.mark.unit]


def test_tool_is_registered_and_main_only() -> None:
    tool = build_workflow_replay_tool()
    assert isinstance(tool, WorkflowReplayTool)
    assert tool.metadata["scope"] == "main_only"
    assert tool.metadata["idempotent_actions"] == ("status",)
    assert any(
        getattr(builder, "__name__", "") == "build_workflow_replay_tool"
        for builder in _MAIN_TOOLS_BUILDERS
    )


@pytest.mark.asyncio
async def test_status_reports_count_and_script(monkeypatch) -> None:
    monkeypatch.setattr(
        tool_mod,
        "resolve_session_plan_identity",
        lambda _sid: type("I", (), {"key": "k1"})(),
    )
    monkeypatch.setattr(tool_mod, "replay_count", lambda _key: 3)
    monkeypatch.setattr(tool_mod, "find_ldw_script", lambda _key: "scripts/ldw__k1.py")

    tool = WorkflowReplayTool()
    payload = json.loads(await tool._arun(action="status", session_id="sess"))

    assert payload == {
        "ok": True,
        "plan_key": "k1",
        "replay_count": 3,
        "script": "scripts/ldw__k1.py",
    }


@pytest.mark.asyncio
async def test_missing_session_fails_closed() -> None:
    tool = WorkflowReplayTool()
    payload = json.loads(await tool._arun(action="status", session_id=""))
    assert payload == {"ok": False, "error": "no session id"}
