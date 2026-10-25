"""Merged TaskFlow surfaces: action routing, validation and a lifecycle e2e.

The merged tools are pure dispatchers over the original module functions.
These tests pin every action's route and required-parameter check without
touching the flow store, then drive create → summary → progress → budget →
finish through the real module implementations.
"""

from __future__ import annotations

import functools
from pathlib import Path
from typing import Annotated, Any, TypedDict

import pytest
from langchain_core.messages import AIMessage
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages
from langgraph.prebuilt import ToolNode

from agent.tools.catalog import TOOL_GROUPS
from agent.tools.parallel_safety import READ_ONLY_TOOLS
from agent.tools.taskflow import build_taskflow_tools
from agent.tools.taskflow.tools import (
    build_legacy_taskflow_tools,
    taskflow_merged,
)

pytestmark = [pytest.mark.unit]

SESSION = "sess-merged-dispatch"


def _tools_by_name() -> dict[str, Any]:
    return {tool.name: tool for tool in build_taskflow_tools()}


async def _invoke(tool: Any, **kwargs: Any) -> str:
    """Call a merged BaseTool the way ToolNode does: ainvoke with injected state."""
    return await tool.ainvoke({**kwargs, "session_id": SESSION})


def _required_value(name: str) -> Any:
    if name == "flow_id":
        return "flow-merged"
    if name == "goal":
        return "ship the merged tool"
    if name == "task":
        return "do the work"
    if name == "child_session_key":
        return "agent:main:subagent:merged-child"
    if name == "result":
        return "child result"
    if name == "step_ids":
        return ["step-1"]
    if name == "steps":
        return [
            {
                "step_id": "step-1",
                "task": "do the work",
                "depends_on": [],
                "status": "ready",
            }
        ]
    if name in {"status_filter", "budget_action"}:
        return "query"
    return "value"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("group", "action"),
    [
        *[("flow", action) for action in taskflow_merged.FLOW_ACTIONS],
        *[("step", action) for action in taskflow_merged.STEP_ACTIONS],
        *[("inspect", action) for action in taskflow_merged.INSPECT_ACTIONS],
    ],
)
async def test_every_action_routes_to_its_module_tool(
    group: str, action: str, monkeypatch: pytest.MonkeyPatch
):
    tools = _tools_by_name()
    merged = {
        "flow": tools["taskflow_flow"],
        "step": tools["taskflow_step"],
        "inspect": tools["taskflow_inspect"],
    }[group]
    module = _module_tool_for_action(action)
    _accepted, required = taskflow_merged._accepted_and_required(module, action)
    kwargs = {name: _required_value(name) for name in required}
    if action == "resume":
        kwargs["child_session_key"] = "agent:main:subagent:merged-child"
    seen: dict[str, Any] = {}

    @functools.wraps(module.coroutine)
    async def fake(**payload: Any) -> str:
        seen.update(payload)
        return f"ok:{action}"

    monkeypatch.setattr(module, "coroutine", fake)

    out = await _invoke(merged, action=action, **kwargs)

    assert out == f"ok:{action}", out
    assert seen["session_id"] == SESSION
    assert "action" not in seen or action == "budget"


def _module_tool_for_action(action: str):
    for group in (
        taskflow_merged._FLOW_TOOLS,
        taskflow_merged._STEP_TOOLS,
        taskflow_merged._INSPECT_TOOLS,
    ):
        if action in group:
            return group[action]
    raise AssertionError(f"no module tool for {action}")


@pytest.mark.asyncio
async def test_budget_action_name_is_mapped(monkeypatch: pytest.MonkeyPatch):
    tools = _tools_by_name()
    module = taskflow_merged._FLOW_TOOLS["budget"]
    seen: dict[str, Any] = {}

    @functools.wraps(module.coroutine)
    async def fake(**payload: Any) -> str:
        seen.update(payload)
        return "budget ok"

    monkeypatch.setattr(module, "coroutine", fake)

    out = await _invoke(
        tools["taskflow_flow"],
        action="budget",
        flow_id="flow-merged",
        budget_action="set",
        token_budget=500,
    )

    assert out == "budget ok"
    assert seen["action"] == "set"
    assert seen["token_budget"] == 500
    assert "budget_action" not in seen


@pytest.mark.asyncio
async def test_budget_query_alias_routes_to_the_budget_action(monkeypatch: pytest.MonkeyPatch):
    """A model that remembers the old taskflow_budget(action='query') shape works."""
    tools = _tools_by_name()
    module = taskflow_merged._FLOW_TOOLS["budget"]
    seen: dict[str, Any] = {}

    @functools.wraps(module.coroutine)
    async def fake(**payload: Any) -> str:
        seen.update(payload)
        return "budget ok"

    monkeypatch.setattr(module, "coroutine", fake)

    out = await _invoke(tools["taskflow_flow"], action="query", flow_id="flow-merged")

    assert out == "budget ok"
    assert seen["action"] == "query"
    assert seen["flow_id"] == "flow-merged"


@pytest.mark.asyncio
async def test_budget_alias_on_the_wrong_group_names_the_budget_call():
    tools = _tools_by_name()

    out = await _invoke(tools["taskflow_step"], action="set", flow_id="flow-merged")

    assert "taskflow_flow(action='budget', budget_action='set')" in out


def test_tool_descriptions_name_each_action_and_its_required_arguments():
    """The flat schema cannot mark action-specific required fields; the
    description does, so a first call does not have to fail to learn them."""
    tools = _tools_by_name()
    descriptions = {name: tool.description for name, tool in tools.items()}

    assert "create→flow_id" in descriptions["taskflow_flow"]
    assert "budget→flow_id" in descriptions["taskflow_flow"]
    assert "run_task→flow_id,task" in descriptions["taskflow_step"]
    assert "resume→flow_id,child_session_key or result" in descriptions["taskflow_step"]
    assert "summary (flow_id)" in descriptions["taskflow_inspect"]


def test_ambiguous_fields_carry_descriptions():
    schemas = {
        tool.name: tool.args_schema.model_json_schema()["properties"]
        for tool in _tools_by_name().values()
    }

    for field in ("flow_id", "expected_revision", "budget_action", "token_budget"):
        assert schemas["taskflow_flow"][field].get("description"), field
    for field in (
        "depends_on",
        "file_deps",
        "response_schema",
        "structured_result",
        "auto_dispatch",
        "steps",
        "step_ids",
    ):
        assert schemas["taskflow_step"][field].get("description"), field
    assert schemas["taskflow_inspect"]["status_filter"].get("description")


@pytest.mark.asyncio
async def test_irrelevant_parameters_are_filtered_before_the_module_call(
    monkeypatch: pytest.MonkeyPatch,
):
    tools = _tools_by_name()
    module = taskflow_merged._FLOW_TOOLS["create"]
    seen: dict[str, Any] = {}

    @functools.wraps(module.coroutine)
    async def fake(**payload: Any) -> str:
        seen.update(payload)
        return "created"

    monkeypatch.setattr(module, "coroutine", fake)

    out = await _invoke(
        tools["taskflow_flow"],
        action="create",
        flow_id="flow-merged",
        reason="not a create parameter",
        expected_revision=99,
        budget_action="set",
    )

    assert out == "created"
    assert seen["flow_id"] == "flow-merged"
    assert "reason" not in seen and "expected_revision" not in seen
    assert "action" not in seen, "budget_action must not leak into a non-budget action"


@pytest.mark.asyncio
async def test_run_task_forwards_the_new_file_deps_field(monkeypatch: pytest.MonkeyPatch):
    tools = _tools_by_name()
    module = taskflow_merged._STEP_TOOLS["run_task"]
    seen: dict[str, Any] = {}

    @functools.wraps(module.coroutine)
    async def fake(**payload: Any) -> str:
        seen.update(payload)
        return "registered"

    monkeypatch.setattr(module, "coroutine", fake)

    out = await _invoke(
        tools["taskflow_step"],
        action="run_task",
        flow_id="flow-merged",
        task="verify alpha",
        file_deps=["alpha.py"],
    )

    assert out == "registered"
    assert seen["file_deps"] == ["alpha.py"]


@pytest.mark.asyncio
async def test_missing_required_parameter_returns_a_correction_hint():
    tools = _tools_by_name()

    out = await _invoke(tools["taskflow_step"], action="run_task", flow_id="flow-merged")

    assert "missing required parameter(s): task" in out
    assert "Accepted parameters:" in out


@pytest.mark.asyncio
async def test_resume_requires_the_child_key_or_result():
    tools = _tools_by_name()

    out = await _invoke(tools["taskflow_step"], action="resume", flow_id="flow-merged")

    assert "child_session_key" in out and "result" in out
    assert " or " in out


@pytest.mark.asyncio
async def test_resume_accepts_a_result_without_a_child_key(monkeypatch: pytest.MonkeyPatch):
    """The module contract is one-of (child key or result), not both."""
    tools = _tools_by_name()
    module = taskflow_merged._STEP_TOOLS["resume"]
    seen: dict[str, Any] = {}

    @functools.wraps(module.coroutine)
    async def fake(**payload: Any) -> str:
        seen.update(payload)
        return "resumed"

    monkeypatch.setattr(module, "coroutine", fake)

    out = await _invoke(
        tools["taskflow_step"],
        action="resume",
        flow_id="flow-merged",
        result="child finished",
    )

    assert out == "resumed"
    assert seen["result"] == "child finished"


@pytest.mark.asyncio
async def test_wrong_group_names_the_right_tool():
    tools = _tools_by_name()

    out = await _invoke(
        tools["taskflow_flow"],
        action="resume",
        flow_id="flow-merged",
        child_session_key="child",
        result="done",
    )

    assert "taskflow_step(action='resume')" in out


@pytest.mark.asyncio
async def test_unknown_action_lists_the_group_actions():
    tools = _tools_by_name()

    out = await _invoke(tools["taskflow_inspect"], action="summary2")

    assert "unknown action 'summary2'" in out
    assert "summary" in out and "progress" in out and "list" in out


def test_catalog_and_metadata_are_the_three_surfaces():
    assert TOOL_GROUPS["tasks"][:3] == ("taskflow_flow", "taskflow_step", "taskflow_inspect")
    assert "taskflow_summary" not in TOOL_GROUPS["tasks"]
    assert "taskflow_inspect" in READ_ONLY_TOOLS

    tools = _tools_by_name()
    assert set(tools) == {"taskflow_flow", "taskflow_step", "taskflow_inspect"}
    assert tools["taskflow_inspect"].metadata["idempotent"] is True
    assert tools["taskflow_inspect"].metadata["side_effect_scope"] == "none"
    assert tools["taskflow_flow"].metadata["idempotent"] is False
    assert tools["taskflow_step"].metadata["scope"] == "main_only"


def test_the_legacy_builder_keeps_the_sixteen_module_tools_for_tests():
    tools = {tool.name: tool for tool in build_legacy_taskflow_tools()}

    assert len(tools) == 16
    assert "taskflow_create" in tools and "taskflow_run_task" in tools
    # The adapter preserves the original read/write split, not the merged one.
    assert tools["taskflow_summary"].metadata["idempotent"] is True
    assert tools["taskflow_progress"].metadata["idempotent"] is True
    assert tools["taskflow_list"].metadata["idempotent"] is True
    assert tools["taskflow_create"].metadata["idempotent"] is False


@pytest.mark.asyncio
async def test_toolnode_injects_session_state_into_the_merged_dispatch(
    monkeypatch: pytest.MonkeyPatch,
):
    """Production reaches the merged tool through ToolNode, not a manual ainvoke."""
    tool = _tools_by_name()["taskflow_flow"]
    module = taskflow_merged._FLOW_TOOLS["create"]
    seen: dict[str, Any] = {}

    @functools.wraps(module.coroutine)
    async def fake(**payload: Any) -> str:
        seen.update(payload)
        return "created"

    monkeypatch.setattr(module, "coroutine", fake)

    class _State(TypedDict):
        messages: Annotated[list, add_messages]
        session_id: str

    graph = StateGraph(_State)
    graph.add_node("tools", ToolNode([tool]))
    graph.add_edge(START, "tools")
    graph.add_edge("tools", END)
    app = graph.compile()
    call = {
        "name": "taskflow_flow",
        "args": {"action": "create", "flow_id": "flow-injected"},
        "id": "call-1",
        "type": "tool_call",
    }

    await app.ainvoke(
        {
            "messages": [AIMessage(content="", tool_calls=[call])],
            "session_id": SESSION,
        }
    )

    assert seen["flow_id"] == "flow-injected"
    assert seen["session_id"] == SESSION


@pytest.mark.asyncio
async def test_create_summary_progress_budget_finish_lifecycle(isolated_db: Path):
    tools = _tools_by_name()
    flow = tools["taskflow_flow"]
    inspect = tools["taskflow_inspect"]
    steps = [
        {
            "step_id": "step-1",
            "task": "already done",
            "depends_on": [],
            "status": "done",
        }
    ]

    created = await _invoke(
        flow,
        action="create",
        flow_id="merged-lifecycle",
        initial_state={"steps": steps},
    )
    assert "TaskFlow created" in created

    summary = await _invoke(inspect, action="summary", flow_id="merged-lifecycle")
    assert "TaskFlow summary" in summary and "status=done" not in summary

    progress = await _invoke(inspect, action="progress", flow_id="merged-lifecycle")
    assert "Progress Report: merged-lifecycle" in progress

    board = await _invoke(inspect, action="list", status_filter="all")
    assert "merged-lifecycle" in board

    budget_set = await _invoke(
        flow,
        action="budget",
        flow_id="merged-lifecycle",
        budget_action="set",
        token_budget=1234,
    )
    assert "1,234" in budget_set
    budget_query = await _invoke(flow, action="budget", flow_id="merged-lifecycle")
    assert "1,234" in budget_query

    finished = await _invoke(flow, action="finish", flow_id="merged-lifecycle")
    assert "TaskFlow finished" in finished
