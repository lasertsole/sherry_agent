"""LDW leaf construction: role tool face and structured output."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from langchain.agents.structured_output import ToolStrategy
from langchain_core.runnables import RunnableLambda

from agent.tools.ldw import leaf as leaf_mod
from agent.tools.ldw.compiler import compile_plan_to_workflow
from agent.tools.ldw.leaf import _leaf_tools, make_leaf_runnable, register_roster
from agent.tools.subagent.ldw_shim import to_pydantic_model

pytestmark = [pytest.mark.unit, pytest.mark.timeout(60)]


def test_leaf_tools_uses_the_child_assembly_contract(monkeypatch) -> None:
    captured: dict = {}

    def _assemble(tools, allow, deny, extra, role, session_id):
        captured.update(
            tools=tools, allow=allow, deny=deny, extra=extra, role=role, session_id=session_id
        )
        return [SimpleNamespace(name="read_file")]

    monkeypatch.setattr("agent.tools.subagent.spawn.core._assemble_child_tools", _assemble)
    tools = _leaf_tools({"functional_role": "executor", "extra_tools": ["web_fetch"]}, "sess")

    assert [tool.name for tool in tools] == ["read_file"]
    assert captured["tools"] is None
    assert captured["deny"] is None
    assert captured["extra"] == ["web_fetch"]
    assert captured["role"].value == "executor"
    assert captured["session_id"] == "sess"
    assert "read_file" in captured["allow"]


def test_role_allow_list_is_forwarded(monkeypatch) -> None:
    captured: dict = {}

    def _assemble(_tools, allow, _deny, _extra, _role, _session_id):
        captured["allow"] = allow
        return []

    monkeypatch.setattr("agent.tools.subagent.spawn.core._assemble_child_tools", _assemble)
    _leaf_tools({"functional_role": "researcher"}, "sess")

    assert "read_file" in captured["allow"]
    assert "write_file" not in captured["allow"]


def test_general_role_inherits_all_tools(monkeypatch) -> None:
    captured: dict = {}

    def _assemble(_tools, allow, _deny, _extra, _role, _session_id):
        captured["allow"] = allow
        return []

    monkeypatch.setattr("agent.tools.subagent.spawn.core._assemble_child_tools", _assemble)
    _leaf_tools({"functional_role": "general"}, "sess")

    assert captured["allow"] is None


def test_schema_step_registers_a_builder(monkeypatch) -> None:
    schema = {
        "type": "object",
        "properties": {"answer": {"type": "string"}},
        "required": ["answer"],
        "additionalProperties": False,
    }
    meta = compile_plan_to_workflow(
        {"steps": [{"step_id": "a", "task": "x", "response_schema": schema}]}
    )
    monkeypatch.setattr(
        leaf_mod,
        "_leaf_graph",
        lambda _step, _sid, response_format: (RunnableLambda(lambda x: x), []),
    )
    roster = register_roster(meta, "sess")
    entry = roster.resolve("general@a")
    assert entry.builder is not None
    runnable = entry.builder(response_format=ToolStrategy(to_pydantic_model(schema)))
    assert callable(runnable.ainvoke)


def test_leaf_runnable_is_callable_without_schema(monkeypatch) -> None:
    monkeypatch.setattr(
        leaf_mod,
        "_leaf_graph",
        lambda _step, _sid, response_format: (RunnableLambda(lambda x: x), []),
    )
    runnable = make_leaf_runnable({"step_id": "a", "task_template": "x"}, "sess")
    assert callable(runnable.ainvoke)


@pytest.mark.asyncio
async def test_ptc_success_short_circuits_the_model(monkeypatch) -> None:
    graph_called = False

    async def _graph(_input, config=None):
        nonlocal graph_called
        graph_called = True
        return {"messages": []}

    monkeypatch.setattr(leaf_mod, "_leaf_graph", lambda *_a: (RunnableLambda(_graph), []))
    monkeypatch.setattr("agent.tools.pub_base.session_workspace_root", lambda _sid: None)

    async def _ptc(*_a, **_k):
        return {"output": "PTC-OK", "calls_log": [{"ok": True}]}, None

    monkeypatch.setattr("agent.tools.subagent.spawn.core._try_ptc_prefill", _ptc)
    runnable = make_leaf_runnable(
        {"step_id": "a", "task_template": "x", "functional_role": "executor"}, "sess"
    )
    from langchain_core.messages import HumanMessage

    out = await runnable.ainvoke({"messages": [HumanMessage(content="task")]})

    assert not graph_called
    assert out["messages"][-1].content == "PTC-OK"


@pytest.mark.asyncio
async def test_ptc_failure_falls_back_to_react_with_progress(monkeypatch) -> None:
    captured: dict = {}

    async def _graph(input, config=None):
        captured["text"] = input["messages"][-1].content
        return {"messages": []}

    monkeypatch.setattr(leaf_mod, "_leaf_graph", lambda *_a: (RunnableLambda(_graph), []))
    monkeypatch.setattr("agent.tools.pub_base.session_workspace_root", lambda _sid: None)

    async def _ptc(*_a, **_k):
        return None, "[PTC Pre-run Progress]"

    monkeypatch.setattr("agent.tools.subagent.spawn.core._try_ptc_prefill", _ptc)
    runnable = make_leaf_runnable(
        {"step_id": "a", "task_template": "x", "functional_role": "executor"}, "sess"
    )
    from langchain_core.messages import HumanMessage

    await runnable.ainvoke({"messages": [HumanMessage(content="task")]})

    assert "task" in captured["text"]
    assert "[PTC Pre-run Progress]" in captured["text"]


@pytest.mark.asyncio
async def test_ptc_success_with_schema_returns_structured_response(monkeypatch) -> None:
    schema = {
        "type": "object",
        "properties": {"answer": {"type": "string"}},
        "required": ["answer"],
        "additionalProperties": False,
    }

    async def _graph(_input, config=None):
        raise AssertionError("PTC success must not call the graph")

    monkeypatch.setattr(leaf_mod, "_leaf_graph", lambda *_a: (RunnableLambda(_graph), []))
    monkeypatch.setattr("agent.tools.pub_base.session_workspace_root", lambda _sid: None)

    async def _ptc(*_a, **_k):
        return {"output": '{"answer": "ok"}', "calls_log": [{"ok": True}]}, None

    monkeypatch.setattr("agent.tools.subagent.spawn.core._try_ptc_prefill", _ptc)
    runnable = make_leaf_runnable(
        {
            "step_id": "a",
            "task_template": "x",
            "functional_role": "executor",
            "response_schema": schema,
        },
        "sess",
    )
    from langchain_core.messages import HumanMessage

    out = await runnable.ainvoke({"messages": [HumanMessage(content="task")]})

    assert out["structured_response"].answer == "ok"
