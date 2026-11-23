"""LDW leaf guarded tool face: the gate on a graph with no middleware chain."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from langchain_core.tools import BaseTool, tool

from agent.tools.ldw import leaf as leaf_mod
from agent.tools.ldw.guarded_face import guarded_tool_face
from agent.tools.pub_base import high_risk_gate as gate_mod
from agent.tools.pub_base.high_risk_guard_store import HighRiskGuardStore
from config.features import HIGH_RISK_GUARD

pytestmark = [pytest.mark.unit, pytest.mark.timeout(60)]

SESSION = "leaf-session"


def _make_tool(name: str, metadata: dict | None = None):
    calls: list[dict] = []

    @tool(name)
    def _run(commands: list[str]) -> str:
        """Run the requested commands."""
        calls.append({"commands": list(commands)})
        return "ran"

    _run.metadata = {"high_risk": False, "idempotent": False, "scan_keywords": True}
    _run.metadata.update(metadata or {})
    return _run, calls


@pytest.fixture(autouse=True)
def _guard_db(tmp_path, monkeypatch):
    path = tmp_path / "guard.db"
    monkeypatch.setenv("SHERRY_HIGH_RISK_GUARD_PATH", str(path))
    return HighRiskGuardStore(path=str(path))


def test_the_wrapper_preserves_the_tool_contract() -> None:
    raw, _calls = _make_tool("terminal")
    wrapped = guarded_tool_face([raw], SESSION)[0]

    assert isinstance(wrapped, BaseTool)
    assert wrapped.name == raw.name
    assert wrapped.description == raw.description
    assert wrapped.metadata == raw.metadata
    assert set(wrapped.args_schema.model_fields) == set(raw.args_schema.model_fields)


def test_a_clean_call_runs_and_is_ledgered() -> None:
    raw, calls = _make_tool("mcp__stripe__charge", {"high_risk": True})
    wrapped = guarded_tool_face([raw], SESSION)[0]

    out = asyncio.run(wrapped.ainvoke({"commands": ["charge"]}))

    assert out == "ran"
    assert calls == [{"commands": ["charge"]}]
    from agent.tools.pub_base.high_risk_guard_store import default_store

    row = default_store().check_op(SESSION, "mcp__stripe__charge", {"commands": ["charge"]})
    assert row is not None and row["status"] == "committed"


def test_a_keyword_hit_returns_the_refusal_and_does_not_run() -> None:
    raw, calls = _make_tool("terminal")
    wrapped = guarded_tool_face([raw], SESSION)[0]

    out = asyncio.run(wrapped.ainvoke({"commands": ["rm -rf /tmp/x"]}))

    assert isinstance(out, str) and "NEEDS_APPROVAL" in out
    assert calls == []


def test_an_ordinary_tool_never_reads_the_store(monkeypatch) -> None:
    raw, _calls = _make_tool("read_file", {"idempotent": True, "scan_keywords": False})
    wrapped = guarded_tool_face([raw], SESSION)[0]
    monkeypatch.setattr(
        gate_mod,
        "default_store",
        lambda: (_ for _ in ()).throw(AssertionError("store touched")),
    )

    assert asyncio.run(wrapped.ainvoke({"commands": ["a.py"]})) == "ran"


def test_the_escape_switch_returns_the_same_objects(monkeypatch) -> None:
    raw, _calls = _make_tool("terminal")
    monkeypatch.setitem(HIGH_RISK_GUARD, "replay_path_gate_enabled", False)

    face = guarded_tool_face([raw], SESSION)

    assert face == [raw] and face[0] is raw


def test_a_non_basetool_is_passed_through() -> None:
    fake = SimpleNamespace(name="read_file")

    face = guarded_tool_face([fake], SESSION)

    assert face[0] is fake


def test_the_leaf_graph_guards_the_face_it_hands_to_create_agent(monkeypatch) -> None:
    raw, _calls = _make_tool("terminal")
    captured: dict = {}

    monkeypatch.setattr(leaf_mod, "_leaf_tools", lambda _step, _sid: [raw])
    monkeypatch.setattr(leaf_mod, "_leaf_model", lambda *_a, **_k: object())
    monkeypatch.setattr(leaf_mod, "_system_prompt", lambda _step: "prompt")

    def _create_agent(**kwargs):
        captured.update(kwargs)
        return object()

    monkeypatch.setattr(leaf_mod, "create_agent", _create_agent)

    graph, tools = leaf_mod._leaf_graph({"functional_role": "executor"}, SESSION, None)

    assert graph is not None
    assert tools == [raw]
    assert captured["tools"] != [raw], "the graph must receive the guarded face"
    assert captured["tools"][0].name == "terminal"
