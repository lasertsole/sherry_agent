"""Unit tests for the todo + boulder injection blocks in workspace/prompt_builder.py.

The todo list (todos.db) and the active-work pointer (.omo/boulder.json) are
rebuilt into the system prompt on EVERY call, so they survive context
compression. Both builders must FAIL-OPEN: a broken store or boulder file must
never prevent the persona prompt from being assembled.

Tests are tests-after glue per the plan; they monkeypatch the sync store reader
and point the boulder path at a tmp file, so no real DB or repo file is touched.
"""

import json

import pytest


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class FakeStateDB:
    """In-memory stand-in for state_register_db (get/set_state only)."""

    def __init__(self):
        self._states: dict[str, dict[str, object]] = {}

    def get_state(self, session_id, key, default=None):
        return self._states.get(session_id, {}).get(key, default)

    def set_state(self, session_id, key, value):
        self._states.setdefault(session_id, {})[key] = value
        return True


class FakeMemoryStore:
    """Stand-in for agent.tools.memory.memory_store."""

    def format_for_system_prompt(self, target: str):
        return "MEMORY-LIVE" if target == "memory" else None


pytestmark = [pytest.mark.unit]


@pytest.fixture
def prompt_env(tmp_path, monkeypatch):
    """Isolate persona files, memory, the todo reader and the boulder path."""
    file_names = ["AGENTS.md", "SOUL.md"]
    (tmp_path / "AGENTS.md").write_text("AGENTS-PERSONA", encoding="utf-8")
    (tmp_path / "SOUL.md").write_text("SOUL-PERSONA", encoding="utf-8")

    monkeypatch.setattr("workspace.prompt_builder.WORKSPACE_DIR", tmp_path)
    monkeypatch.setattr("workspace.prompt_builder.ALL_SYSTEM_FILE_NAMES", file_names)
    monkeypatch.setattr("workspace.prompt_builder.get_skills_text", lambda *a, **k: "SKILLS-BLOCK")
    monkeypatch.setattr("runtime.state_register_db", FakeStateDB())
    monkeypatch.setattr("agent.tools.memory.memory_store", FakeMemoryStore())

    boulder_path = tmp_path / "boulder.json"
    monkeypatch.setattr("workspace.prompt_builder._BOULDER_PATH", boulder_path)

    state: dict[str, object] = {"todos": [], "raise": None, "calls": 0}

    def fake_get_todos_sync(session_id):
        state["calls"] = int(state["calls"]) + 1  # type: ignore[arg-type]
        if state["raise"] is not None:
            raise state["raise"]  # type: ignore[misc]
        return state["todos"]

    monkeypatch.setattr(
        "agent.tools.todolist.registry.store_sqlite.get_todos_sync",
        fake_get_todos_sync,
    )

    return {"dir": tmp_path, "boulder_path": boulder_path, "state": state}


def _seed_todos(prompt_env, todos):
    prompt_env["state"]["todos"] = todos


def _write_boulder(prompt_env, *, work_id="w1", plan=".omo/plans/w1.md", status="active"):
    prompt_env["boulder_path"].write_text(
        json.dumps(
            {
                "schema_version": 2,
                "active_work_id": work_id,
                "works": {
                    work_id: {
                        "work_id": work_id,
                        "active_plan": plan,
                        "status": status,
                    }
                },
            }
        ),
        encoding="utf-8",
    )


def _todo(content, *, status="pending", priority="medium", category="quick", **extra):
    row = {
        "content": content,
        "status": status,
        "priority": priority,
        "category": category,
        "delegation": "self",
        "flow_id": None,
        "step_id": None,
    }
    row.update(extra)
    return row


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestEmptyState:
    def test_no_todos_and_no_boulder_renders_neither_block(self, prompt_env):
        from workspace.prompt_builder import build_system_prompt

        prompt = build_system_prompt(session_id="sess-empty")

        assert "## Current Todo List" not in prompt
        assert "## Active Work" not in prompt
        assert "AGENTS-PERSONA" in prompt
        assert "SKILLS-BLOCK" in prompt


class TestTodoBlock:
    def test_seeded_todos_render_header_and_item_text(self, prompt_env):
        from workspace.prompt_builder import build_system_prompt

        _seed_todos(
            prompt_env,
            [
                _todo("Implement the widget", status="in_progress", priority="high"),
                _todo("Review the widget", status="pending"),
            ],
        )

        prompt = build_system_prompt(session_id="sess-todos")

        assert "## Current Todo List" in prompt
        assert "Implement the widget" in prompt
        assert "Review the widget" in prompt
        # status icon + priority are rendered
        assert "[◐]" in prompt
        assert "[○]" in prompt
        assert "(high)" in prompt

    def test_flow_and_step_are_tagged_when_present(self, prompt_env):
        from workspace.prompt_builder import build_system_prompt

        _seed_todos(
            prompt_env,
            [
                _todo(
                    "Dispatch step",
                    category="deep",
                    delegation="subagent",
                    flow_id="flow-7",
                    step_id="step-2",
                )
            ],
        )

        prompt = build_system_prompt(session_id="sess-flow")

        assert "flow-7" in prompt
        assert "step-2" in prompt
        assert "deep" in prompt
        assert "subagent" in prompt

    def test_notice_lines_are_present_when_todos_exist(self, prompt_env):
        from workspace.prompt_builder import build_system_prompt

        _seed_todos(prompt_env, [_todo("Tracked work")])

        prompt = build_system_prompt(session_id="sess-notices")

        assert "tracked by the continuation system" in prompt
        assert "Sisyphus contract" in prompt


class TestBoulderBlock:
    def test_active_boulder_renders_plan_and_remaining_count(self, prompt_env):
        from workspace.prompt_builder import build_system_prompt

        _write_boulder(prompt_env, work_id="w1", plan=".omo/plans/todolist-phase1.md")
        _seed_todos(
            prompt_env,
            [
                _todo("one", status="pending"),
                _todo("two", status="in_progress"),
                _todo("three", status="completed"),
                _todo("four", status="cancelled"),
            ],
        )

        prompt = build_system_prompt(session_id="sess-boulder")

        assert "## Active Work" in prompt
        assert ".omo/plans/todolist-phase1.md" in prompt
        # pending + in_progress only -> 2 remaining
        assert "Remaining: 2" in prompt

    def test_paused_boulder_still_renders(self, prompt_env):
        from workspace.prompt_builder import build_system_prompt

        _write_boulder(prompt_env, plan=".omo/plans/paused.md", status="paused")
        _seed_todos(prompt_env, [_todo("still pending")])

        prompt = build_system_prompt(session_id="sess-paused")

        assert "## Active Work" in prompt
        assert ".omo/plans/paused.md" in prompt

    def test_completed_boulder_renders_nothing(self, prompt_env):
        from workspace.prompt_builder import build_system_prompt

        _write_boulder(prompt_env, plan=".omo/plans/done.md", status="completed")

        prompt = build_system_prompt(session_id="sess-done")

        assert "## Active Work" not in prompt


class TestFailOpen:
    def test_store_exception_still_builds_persona_prompt(self, prompt_env):
        from workspace.prompt_builder import build_system_prompt

        prompt_env["state"]["raise"] = RuntimeError("store exploded")

        prompt = build_system_prompt(session_id="sess-broken")

        assert "AGENTS-PERSONA" in prompt
        assert "SKILLS-BLOCK" in prompt
        assert "## Current Todo List" not in prompt

    def test_malformed_boulder_json_still_builds_persona_prompt(self, prompt_env):
        from workspace.prompt_builder import build_system_prompt

        prompt_env["boulder_path"].write_text("{not valid json", encoding="utf-8")

        prompt = build_system_prompt(session_id="sess-bad-boulder")

        assert "AGENTS-PERSONA" in prompt
        assert "## Active Work" not in prompt


class TestSessionGuard:
    def test_none_session_skips_store_and_boulder(self, prompt_env):
        from workspace.prompt_builder import build_system_prompt

        _write_boulder(prompt_env, plan=".omo/plans/should-not-show.md")
        _seed_todos(prompt_env, [_todo("should not show")])

        prompt = build_system_prompt(session_id=None)

        assert prompt_env["state"]["calls"] == 0
        assert "## Current Todo List" not in prompt
        assert "## Active Work" not in prompt
        assert "AGENTS-PERSONA" in prompt

    def test_empty_session_skips_store_and_boulder(self, prompt_env):
        from workspace.prompt_builder import build_system_prompt

        _write_boulder(prompt_env, plan=".omo/plans/should-not-show.md")
        _seed_todos(prompt_env, [_todo("should not show")])

        prompt = build_system_prompt(session_id="")

        assert prompt_env["state"]["calls"] == 0
        assert "## Current Todo List" not in prompt
        assert "## Active Work" not in prompt
