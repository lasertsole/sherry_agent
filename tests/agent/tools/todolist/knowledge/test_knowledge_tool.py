"""Unit tests for the knowledge tool: write/read/list actions + metadata contract.

The tool is exercised through ``build_knowledge_tools()`` so the assertions run
against the same object the agent is wired with. The store root is redirected
to ``tmp_path``; no real workspace knowledge tree is touched.
"""

from pathlib import Path

import pytest

from agent.tools.todolist.knowledge import build_knowledge_tools

pytestmark = [pytest.mark.unit]


@pytest.fixture()
def knowledge_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point the store at a per-test knowledge root."""
    root = tmp_path / "plans"
    monkeypatch.setattr(
        "agent.tools.todolist.knowledge.knowledge_store._KNOWLEDGE_ROOT",
        root,
    )
    return root


@pytest.fixture()
def tool():
    return {t.name: t for t in build_knowledge_tools()}["knowledge"]


def test_metadata_marks_nudge_and_main_only(tool):
    assert tool.metadata["nudge"] is True
    assert tool.metadata["scope"] == "main_only"
    assert tool.handle_tool_error is True


class TestWriteAction:
    @pytest.mark.asyncio
    async def test_write_persists_plan_summary_and_reports_path(self, tool, knowledge_root: Path):
        output = await tool.coroutine(
            action="write",
            plan_name="implement-auth",
            layer="plan",
            data={"method": "async-hashing"},
        )

        assert output.startswith("Knowledge written to ")
        assert (knowledge_root / "implement-auth" / "plan-summary.json").is_file()

    @pytest.mark.asyncio
    async def test_write_missing_params_returns_error_text(self, tool):
        output = await tool.coroutine(action="write", plan_name="p")

        assert output == "Error: write requires plan_name, layer, and data"

    @pytest.mark.asyncio
    async def test_write_task_layer_without_position_returns_error_text(
        self, tool, knowledge_root: Path
    ):
        output = await tool.coroutine(action="write", plan_name="p", layer="task", data={"m": 1})

        assert output == "Error: position is required for the task layer"


class TestReadAction:
    @pytest.mark.asyncio
    async def test_read_without_plan_name_returns_error_text(self, tool):
        output = await tool.coroutine(action="read")

        assert output == "Error: read requires plan_name"

    @pytest.mark.asyncio
    async def test_read_returns_formatted_task_detail(self, tool, knowledge_root: Path):
        await tool.coroutine(
            action="write",
            plan_name="p",
            layer="task",
            position=0,
            data={"method": "m", "failure_set": ["boom"]},
        )

        output = await tool.coroutine(action="read", plan_name="p", layer="task", position=0)

        assert "# Task 0 Knowledge: p" in output
        assert "boom" in output

    @pytest.mark.asyncio
    async def test_read_missing_plan_returns_not_found_text(self, tool, knowledge_root: Path):
        output = await tool.coroutine(action="read", plan_name="nope")

        assert output == "No knowledge found for plan: nope"


class TestListAction:
    @pytest.mark.asyncio
    async def test_list_empty_returns_no_files_text(self, tool, knowledge_root: Path):
        output = await tool.coroutine(action="list")

        assert output == "No knowledge files found."

    @pytest.mark.asyncio
    async def test_list_reports_plan_and_method(self, tool, knowledge_root: Path):
        await tool.coroutine(
            action="write", plan_name="alpha", layer="plan", data={"method": "m-alpha"}
        )

        output = await tool.coroutine(action="list")

        assert "Available plans with knowledge:" in output
        assert "alpha (method: m-alpha)" in output
