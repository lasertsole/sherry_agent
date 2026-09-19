"""Unit tests for the knowledge tool: write/read/list actions + metadata contract.

The tool is exercised through ``build_knowledge_tools()`` so the assertions run
against the same object the agent is wired with. The store root is redirected
to ``tmp_path``; no real workspace knowledge tree is touched. Every call passes
the session id and the fixture associates it with the plan under test, matching
the production InjectedState contract (see ``test_knowledge_ownership.py`` for
the isolation cases themselves).
"""

from pathlib import Path

import pytest

from agent.tools.todolist.knowledge import build_knowledge_tools, ownership

pytestmark = [pytest.mark.unit]

SESSION_ID = "sess-knowledge"


class _FakeStateDB:
    """In-memory stand-in for state_register_db (get/set_state only)."""

    def __init__(self) -> None:
        self._states: dict[str, dict[str, object]] = {}

    def get_state(self, session_id: str, key: str, default: object = None) -> object:
        return self._states.get(session_id, {}).get(key, default)

    def set_state(self, session_id: str, key: str, value: object) -> bool:
        self._states.setdefault(session_id, {})[key] = value
        return True


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
def state_db(monkeypatch: pytest.MonkeyPatch) -> _FakeStateDB:
    """Isolate the ownership sources (state / todos / boulder) per test."""
    fake = _FakeStateDB()
    monkeypatch.setattr(ownership, "state_register_db", fake)
    monkeypatch.setattr(ownership, "get_todos_sync", lambda session_id: [])
    monkeypatch.setattr(
        ownership, "resolve_boulder_path", lambda: Path("/nonexistent/boulder.json")
    )
    return fake


@pytest.fixture()
def session_id() -> str:
    return SESSION_ID


def _associate(state_db: _FakeStateDB, plan_name: str, session_id: str = SESSION_ID) -> None:
    """Link the session to a plan through the state plan_ref source."""
    state_db.set_state(session_id, "plan_ref", f".omo/plans/{plan_name}.md")


@pytest.fixture()
def tool():
    return {t.name: t for t in build_knowledge_tools()}["knowledge"]


def test_metadata_marks_nudge_and_main_only(tool):
    assert tool.metadata["nudge"] is True
    assert tool.metadata["scope"] == "main_only"
    assert tool.handle_tool_error is True


class TestWriteAction:
    @pytest.mark.asyncio
    async def test_write_persists_plan_summary_and_reports_path(
        self, tool, knowledge_root: Path, state_db, session_id: str
    ):
        _associate(state_db, "implement-auth")

        output = await tool.coroutine(
            action="write",
            plan_name="implement-auth",
            layer="plan",
            data={"method": "async-hashing"},
            session_id=session_id,
        )

        assert output.startswith("Knowledge written to ")
        written_dirs = [entry for entry in knowledge_root.iterdir() if entry.is_dir()]
        assert len(written_dirs) == 1
        assert (written_dirs[0] / "plan-summary.json").is_file()
        assert (written_dirs[0] / "meta.json").is_file()

    @pytest.mark.asyncio
    async def test_write_missing_params_returns_error_text(self, tool, session_id: str):
        output = await tool.coroutine(action="write", plan_name="p", session_id=session_id)

        assert output == "Error: write requires plan_name, layer, and data"

    @pytest.mark.asyncio
    async def test_write_task_layer_without_position_returns_error_text(
        self, tool, knowledge_root: Path, state_db, session_id: str
    ):
        _associate(state_db, "p")

        output = await tool.coroutine(
            action="write", plan_name="p", layer="task", data={"m": 1}, session_id=session_id
        )

        assert output == "Error: position is required for the task layer"


class TestReadAction:
    @pytest.mark.asyncio
    async def test_read_without_plan_name_returns_error_text(self, tool, session_id: str):
        output = await tool.coroutine(action="read", session_id=session_id)

        assert output == "Error: read requires plan_name"

    @pytest.mark.asyncio
    async def test_read_returns_formatted_task_detail(
        self, tool, knowledge_root: Path, state_db, session_id: str
    ):
        _associate(state_db, "p")
        await tool.coroutine(
            action="write",
            plan_name="p",
            layer="task",
            position=0,
            data={"method": "m", "failure_set": ["boom"]},
            session_id=session_id,
        )

        output = await tool.coroutine(
            action="read", plan_name="p", layer="task", position=0, session_id=session_id
        )

        assert "# Task 0 Knowledge: p" in output
        assert "boom" in output

    @pytest.mark.asyncio
    async def test_read_missing_plan_returns_not_found_text(
        self, tool, knowledge_root: Path, state_db, session_id: str
    ):
        _associate(state_db, "nope")

        output = await tool.coroutine(action="read", plan_name="nope", session_id=session_id)

        assert output == "No knowledge found for plan: nope"


class TestListAction:
    @pytest.mark.asyncio
    async def test_list_empty_returns_no_files_text(
        self, tool, knowledge_root: Path, session_id: str
    ):
        output = await tool.coroutine(action="list", session_id=session_id)

        assert output == "No knowledge files found."

    @pytest.mark.asyncio
    async def test_list_reports_plan_and_method(
        self, tool, knowledge_root: Path, state_db, session_id: str
    ):
        _associate(state_db, "alpha")
        await tool.coroutine(
            action="write",
            plan_name="alpha",
            layer="plan",
            data={"method": "m-alpha"},
            session_id=session_id,
        )

        output = await tool.coroutine(action="list", session_id=session_id)

        assert "Available plans with knowledge:" in output
        assert "alpha" in output
        assert "method: m-alpha" in output
