"""The merged terminal_bg tool: list/output dispatch, metadata and migration.

The two legacy functions stay as handlers; these tests pin the model-facing
action tool, its correction hints, and the catalog/read-only surface.
"""

from __future__ import annotations

from typing import Annotated, TypedDict

import pytest
from langchain_core.messages import AIMessage
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages
from langgraph.prebuilt import ToolNode

from agent.tools import _MAIN_TOOLS_BUILDERS
from agent.tools.catalog import TOOL_GROUPS, tool_catalog, tool_required
from agent.tools.parallel_safety import READ_ONLY_TOOLS
from agent.tools.terminal_bg_tools import (
    build_legacy_terminal_bg_tools,
    build_terminal_bg_tool,
    terminal_bg,
    terminal_bg_list,
    terminal_bg_output,
)

pytestmark = [pytest.mark.unit]

SESSION = "sess-terminal-bg"


def _invoke(**args):
    return terminal_bg.invoke({**args, "session_id": SESSION})


def test_list_dispatches_to_the_legacy_handler(monkeypatch):
    seen: list[str] = []
    monkeypatch.setattr(
        "agent.tools.terminal_bg_tools._list_session_tasks",
        lambda session_id: seen.append(session_id) or "listed",
    )

    assert _invoke(action="list") == "listed"
    assert seen == [SESSION]


def test_output_dispatches_to_the_legacy_handler(monkeypatch):
    seen: list[tuple[str, str]] = []
    monkeypatch.setattr(
        "agent.tools.terminal_bg_tools._read_task_output",
        lambda task_id, session_id: seen.append((task_id, session_id)) or "output",
    )

    assert _invoke(action="output", task_id="bg-1") == "output"
    assert seen == [("bg-1", SESSION)]


def test_action_is_normalized_before_dispatch(monkeypatch):
    monkeypatch.setattr(
        "agent.tools.terminal_bg_tools._list_session_tasks", lambda session_id: "listed"
    )
    assert _invoke(action="  LIST  ") == "listed"


def test_output_requires_a_task_id():
    result = _invoke(action="output")

    assert result.startswith("Error: terminal_bg action='output'")
    assert "task_id" in result


def test_unknown_action_lists_the_two_actions():
    result = _invoke(action="jump")

    assert "unknown action 'jump'" in result
    assert "list" in result and "output" in result


def test_the_legacy_functions_remain_available():
    assert terminal_bg_list.name == "terminal_bg_list"
    assert terminal_bg_output.name == "terminal_bg_output"
    names = {tool.name for tool in build_legacy_terminal_bg_tools()}
    assert names == {"terminal_bg_list", "terminal_bg_output"}


def test_the_merged_builder_is_registered_and_main_only():
    assert build_terminal_bg_tool in _MAIN_TOOLS_BUILDERS
    tool = build_terminal_bg_tool()
    assert tool.name == "terminal_bg"
    assert tool.handle_tool_error is True
    assert tool.metadata == {"scope": "main_only"}


def test_catalog_and_parallel_safety_use_the_merged_name():
    assert TOOL_GROUPS["terminal"] == ("terminal", "python_repl", "terminal_bg")
    entries = tool_catalog([build_terminal_bg_tool()])
    assert {entry["name"] for entry in entries} == {"terminal_bg"}
    assert {entry["group"] for entry in entries} == {"terminal"}
    assert tool_required("terminal_bg") is False
    assert "terminal_bg" in READ_ONLY_TOOLS
    assert "terminal_bg_list" not in READ_ONLY_TOOLS
    assert "terminal_bg_output" not in READ_ONLY_TOOLS


def test_toolnode_injects_the_session(monkeypatch):
    seen: list[str] = []
    monkeypatch.setattr(
        "agent.tools.terminal_bg_tools._list_session_tasks",
        lambda session_id: seen.append(session_id) or "listed",
    )

    class _State(TypedDict):
        messages: Annotated[list, add_messages]
        session_id: str

    graph = StateGraph(_State)
    graph.add_node("tools", ToolNode([build_terminal_bg_tool()]))
    graph.add_edge(START, "tools")
    graph.add_edge("tools", END)
    app = graph.compile()
    call = {
        "name": "terminal_bg",
        "args": {"action": "list"},
        "id": "call-1",
        "type": "tool_call",
    }

    result = app.invoke(
        {
            "messages": [AIMessage(content="", tool_calls=[call])],
            "session_id": SESSION,
        }
    )

    assert seen == [SESSION]
    assert result["messages"][-1].content == "listed"
