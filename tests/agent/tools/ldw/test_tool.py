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
    monkeypatch.setattr(
        tool_mod,
        "plan_knowledge_from_flow",
        lambda _sid, **_kwargs: {"flow_id": "flow-1"},
    )

    tool = WorkflowReplayTool()
    payload = json.loads(await tool._arun(action="status", session_id="sess"))

    assert payload["ok"] is True
    assert payload["plan_key"] == "k1"
    assert payload["replay_count"] == 3
    assert payload["script"] == "scripts/ldw__k1.py"
    assert payload["has_flow"] is True
    assert payload["flow_id"] == "flow-1"


@pytest.mark.asyncio
async def test_missing_session_fails_closed() -> None:
    tool = WorkflowReplayTool()
    payload = json.loads(await tool._arun(action="status", session_id=""))
    assert payload == {"ok": False, "error": "no session id"}


@pytest.mark.asyncio
async def test_replay_marks_degraded_as_ok(monkeypatch) -> None:
    monkeypatch.setattr(
        tool_mod,
        "resolve_session_plan_identity",
        lambda _sid: type("I", (), {"key": "k1"})(),
    )

    async def _replay(*_a, **_k):
        return {"status": "degraded", "plan_key": "k1", "degraded": {"flow_id": "f"}}

    monkeypatch.setattr(tool_mod, "replay_plan", _replay)

    tool = WorkflowReplayTool()
    payload = json.loads(await tool._arun(action="replay", session_id="sess"))

    assert payload["ok"] is True
    assert payload["status"] == "degraded"
    assert payload["degraded"]["flow_id"] == "f"


@pytest.mark.asyncio
async def test_replay_marks_partial_as_not_ok(monkeypatch) -> None:
    monkeypatch.setattr(
        tool_mod,
        "resolve_session_plan_identity",
        lambda _sid: type("I", (), {"key": "k1"})(),
    )

    async def _replay(*_a, **_k):
        return {"status": "partial", "plan_key": "k1", "failed_steps": ["b"]}

    monkeypatch.setattr(tool_mod, "replay_plan", _replay)

    payload = json.loads(await WorkflowReplayTool()._arun(action="replay", session_id="sess"))

    assert payload["ok"] is False
    assert payload["failed_steps"] == ["b"]
