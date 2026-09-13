"""Wiring + todoread behavior for the todolist tool family.

Covers acceptance checks:
1. ``build_main_tools()`` exposes todowrite AND todoread AND still exposes the
   ten taskflow_* tools (registration ADDS, never replaces).
2. every todolist tool carries ``handle_tool_error=True`` and
   ``metadata={"scope": "main_only"}``.
3. todoread returns the JSON list, or ``"No todos found."`` when empty.
4. the builder appends the E2 ``_TODOWRITE_FORMAT_RULES`` block to the todowrite
   description, exactly once (idempotent across repeated builds).

``build_main_tools_real`` is the stub-tolerant loader from conftest: the real
``_MAIN_TOOLS_BUILDERS`` wiring is asserted even when the subagent suite's
``lambda: []`` stub is installed in the same process.
"""

import json
import sys

import pytest

from agent.tools.todolist.tools import build_todolist_tools

tools_module = sys.modules["agent.tools.todolist.tools"]
todoread_module = sys.modules["agent.tools.todolist.tools.todoread"]

_TASKFLOW_NAMES = [
    "taskflow_create",
    "taskflow_run_task",
    "taskflow_set_waiting",
    "taskflow_resume",
    "taskflow_finish",
    "taskflow_fail",
    "taskflow_cancel",
    "taskflow_summary",
    "taskflow_dispatch",
    "taskflow_wait_all",
]

pytestmark = [pytest.mark.unit]


def test_build_main_tools_contains_todolist_and_taskflow(build_main_tools_real):
    """Given the main toolset, When built, Then it has both families."""
    names = [t.name for t in build_main_tools_real()]

    assert "todowrite" in names
    assert "todoread" in names
    for taskflow_name in _TASKFLOW_NAMES:
        assert taskflow_name in names, f"taskflow family was dropped: {taskflow_name} missing"


def test_builder_returns_both_tools_with_metadata_and_handle_tool_error():
    tools = build_todolist_tools()
    assert [t.name for t in tools] == ["todowrite", "todoread"]
    for tool in tools:
        assert tool.handle_tool_error is True, f"{tool.name} missing handle_tool_error"
        assert tool.metadata.get("scope") == "main_only", f"{tool.name} scope mismatch"


def test_todowrite_description_includes_e2_rules_exactly_once():
    """The E2 format block is appended once, even across repeated builds."""
    built = {t.name: t for t in build_todolist_tools()}
    description = built["todowrite"].description

    assert description.endswith(tools_module._TODOWRITE_FORMAT_RULES)
    assert description.count(tools_module._TODOWRITE_FORMAT_RULES) == 1

    # Structural markers the model/parser dispatch on.
    assert "WHERE" in description
    assert "EXPECTED RESULT" in description
    assert "in_progress" in description
    assert "flow_id" in description
    assert "step_id" in description

    # Idempotent: a second build must not duplicate the appended block.
    again = {t.name: t for t in build_todolist_tools()}["todowrite"].description
    assert again == description
    assert again.count(tools_module._TODOWRITE_FORMAT_RULES) == 1


@pytest.mark.asyncio
async def test_todoread_returns_no_todos_found_when_empty(monkeypatch: pytest.MonkeyPatch):
    async def _empty(_session_id: str) -> list[dict]:
        return []

    monkeypatch.setattr(todoread_module.service.TodoService, "get_todos", staticmethod(_empty))
    tools = {t.name: t for t in build_todolist_tools()}

    output = await tools["todoread"].coroutine(session_id="sess-empty")

    assert output == "No todos found."


@pytest.mark.asyncio
async def test_todoread_returns_json_list_when_populated(monkeypatch: pytest.MonkeyPatch):
    todos = [{"content": "step", "status": "in_progress", "priority": "high"}]

    async def _populated(_session_id: str) -> list[dict]:
        return todos

    monkeypatch.setattr(todoread_module.service.TodoService, "get_todos", staticmethod(_populated))
    tools = {t.name: t for t in build_todolist_tools()}

    output = await tools["todoread"].coroutine(session_id="sess-full")

    assert json.loads(output) == todos
