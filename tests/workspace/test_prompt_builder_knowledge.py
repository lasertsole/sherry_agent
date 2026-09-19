"""Unit tests for the Tier-1 knowledge injection in workspace/prompt_builder.py.

``_build_knowledge_block`` resolves the session's plan name (state key first,
todos' plan_ref second), reads ``plan-summary.json`` from the plan knowledge
tree, and renders a compact summary. It is skipped when the caller filters to
explicit files and is fail-open: no plan_ref / missing / malformed summary all
yield "".
"""

import json

import pytest

pytestmark = [pytest.mark.unit]


class FakeStateDB:
    """In-memory stand-in for state_register_db (get/set_state only)."""

    def __init__(self) -> None:
        self._states: dict[str, dict[str, object]] = {}

    def get_state(self, session_id: str, key: str, default: object = None) -> object:
        return self._states.get(session_id, {}).get(key, default)

    def set_state(self, session_id: str, key: str, value: object) -> bool:
        self._states.setdefault(session_id, {})[key] = value
        return True


class FakeMemoryStore:
    """Stand-in for agent.tools.memory.memory_store (no memory content)."""

    def format_for_system_prompt(self, target: str) -> None:
        return None


class FakeFlowStore:
    """Stand-in for the taskflow registry (no active flows)."""

    def get_active_flows_sync(self) -> list[dict]:
        return []


def _write_summary(knowledge_dir, plan_name: str, **fields) -> None:
    plan_dir = knowledge_dir / plan_name
    plan_dir.mkdir(parents=True, exist_ok=True)
    (plan_dir / "plan-summary.json").write_text(
        json.dumps(fields, ensure_ascii=False), encoding="utf-8"
    )


@pytest.fixture
def prompt_env(tmp_path, monkeypatch):
    """Isolate persona files, memory, todos, boulder, knowledge and flow store."""
    file_names = ["AGENTS.md", "SOUL.md"]
    (tmp_path / "AGENTS.md").write_text("AGENTS-PERSONA", encoding="utf-8")
    (tmp_path / "SOUL.md").write_text("SOUL-PERSONA", encoding="utf-8")
    knowledge_dir = tmp_path / "knowledge" / "plans"

    env: dict = {"todos": [], "state": FakeStateDB(), "knowledge_dir": knowledge_dir}

    monkeypatch.setattr("workspace.prompt_builder.WORKSPACE_DIR", tmp_path)
    monkeypatch.setattr("workspace.prompt_builder.ALL_SYSTEM_FILE_NAMES", file_names)
    monkeypatch.setattr("workspace.prompt_builder.get_skills_text", lambda *a, **k: "SKILLS-BLOCK")
    monkeypatch.setattr("runtime.state_register_db", env["state"])
    monkeypatch.setattr("agent.tools.memory.memory_store", FakeMemoryStore())
    monkeypatch.setattr("workspace.prompt_builder._BOULDER_PATH", tmp_path / "boulder.json")
    monkeypatch.setattr(
        "agent.tools.todolist.registry.store_sqlite.get_todos_sync",
        lambda session_id: list(env["todos"]),
    )
    monkeypatch.setattr("agent.tools.todolist.knowledge.ownership.state_register_db", env["state"])
    monkeypatch.setattr(
        "agent.tools.todolist.knowledge.ownership.get_todos_sync",
        lambda session_id: list(env["todos"]),
    )
    monkeypatch.setattr(
        "agent.tools.todolist.knowledge.ownership.resolve_boulder_path",
        lambda: tmp_path / "boulder.json",
    )
    monkeypatch.setattr(
        "agent.tools.todolist.knowledge.knowledge_store._KNOWLEDGE_ROOT", knowledge_dir
    )
    monkeypatch.setattr(
        "agent.tools.taskflow.registry.store_sqlite.get_active_flows_sync",
        FakeFlowStore().get_active_flows_sync,
    )

    return env


class TestKnowledgeBlockInjection:
    def test_no_plan_ref_returns_empty_block(self, prompt_env):
        from workspace.prompt_builder import _build_knowledge_block

        assert _build_knowledge_block("sess-main") == ""

    def test_state_plan_ref_injects_summary(self, prompt_env):
        from workspace.prompt_builder import _build_knowledge_block

        prompt_env["state"].set_state("sess-main", "plan_ref", ".omo/plans/implement-auth.md")
        _write_summary(
            prompt_env["knowledge_dir"],
            "implement-auth",
            method="async-password-hashing",
            key_failures=["sync bcrypt failed"],
            key_successes=["passlib context worked"],
            reusable_patterns=["CryptContext(schemes=['bcrypt'])"],
        )

        block = _build_knowledge_block("sess-main")

        assert block.startswith("## Knowledge Summary: implement-auth")
        assert "Method: async-password-hashing" in block
        assert "Key Failures (avoid repeating):" in block
        assert "sync bcrypt failed" in block
        assert "Key Successes:" in block
        assert "passlib context worked" in block
        assert "Reusable Patterns:" in block
        assert "CryptContext(schemes=['bcrypt'])" in block
        assert "knowledge(action='read')" in block

    def test_todos_plan_ref_is_the_fallback(self, prompt_env):
        from workspace.prompt_builder import _build_knowledge_block

        prompt_env["todos"].append({"content": "x", "plan_ref": ".omo/plans/legacy.md"})
        _write_summary(prompt_env["knowledge_dir"], "legacy", method="legacy-method")

        block = _build_knowledge_block("sess-main")

        assert "Knowledge Summary: legacy" in block
        assert "legacy-method" in block

    def test_state_plan_ref_wins_over_todos(self, prompt_env):
        from workspace.prompt_builder import _build_knowledge_block

        prompt_env["state"].set_state("sess-main", "plan_ref", ".omo/plans/state-plan.md")
        prompt_env["todos"].append({"content": "x", "plan_ref": ".omo/plans/todo-plan.md"})
        _write_summary(prompt_env["knowledge_dir"], "state-plan", method="state-method")
        _write_summary(prompt_env["knowledge_dir"], "todo-plan", method="todo-method")

        block = _build_knowledge_block("sess-main")

        assert "state-method" in block
        assert "todo-method" not in block

    def test_caps_and_truncates_entries(self, prompt_env):
        from workspace.prompt_builder import _build_knowledge_block

        prompt_env["state"].set_state("sess-main", "plan_ref", ".omo/plans/p.md")
        _write_summary(
            prompt_env["knowledge_dir"],
            "p",
            method="m",
            key_failures=[f"failure-{index}" for index in range(1, 7)],
            key_successes=[f"success-{index}" for index in range(1, 7)],
            reusable_patterns=[f"pattern-{index}" for index in range(1, 10)],
        )

        block = _build_knowledge_block("sess-main")

        assert "failure-5" in block
        assert "failure-6" not in block
        assert "success-5" in block
        assert "success-6" not in block
        assert "pattern-8" in block
        assert "pattern-9" not in block

    def test_long_entry_is_single_line_truncated(self, prompt_env):
        from workspace.prompt_builder import _build_knowledge_block

        prompt_env["state"].set_state("sess-main", "plan_ref", ".omo/plans/p.md")
        _write_summary(prompt_env["knowledge_dir"], "p", key_failures=["x" * 500 + "\nmore"])

        block = _build_knowledge_block("sess-main")

        assert "x" * 200 + "..." in block
        assert "x" * 201 not in block
        assert "x" * 200 + "... more" not in block


class TestKnowledgeFailOpen:
    def test_missing_summary_file_returns_empty(self, prompt_env):
        from workspace.prompt_builder import _build_knowledge_block

        prompt_env["state"].set_state("sess-main", "plan_ref", ".omo/plans/ghost.md")

        assert _build_knowledge_block("sess-main") == ""

    def test_malformed_summary_returns_empty(self, prompt_env):
        from workspace.prompt_builder import _build_knowledge_block

        prompt_env["state"].set_state("sess-main", "plan_ref", ".omo/plans/broken.md")
        plan_dir = prompt_env["knowledge_dir"] / "broken"
        plan_dir.mkdir(parents=True)
        (plan_dir / "plan-summary.json").write_text("{not json", encoding="utf-8")

        assert _build_knowledge_block("sess-main") == ""

    def test_state_db_failure_falls_back_to_todos(self, prompt_env, monkeypatch):
        from workspace.prompt_builder import _build_knowledge_block

        class _BoomStateDB:
            def get_state(self, *args, **kwargs):
                raise RuntimeError("state db unavailable")

        monkeypatch.setattr("runtime.state_register_db", _BoomStateDB())
        monkeypatch.setattr(
            "agent.tools.todolist.knowledge.ownership.state_register_db", _BoomStateDB()
        )
        prompt_env["todos"].append({"content": "x", "plan_ref": ".omo/plans/fallback.md"})
        _write_summary(prompt_env["knowledge_dir"], "fallback", method="fallback-method")

        block = _build_knowledge_block("sess-main")

        assert "fallback-method" in block


class TestKnowledgeInSystemPrompt:
    def test_injected_into_system_prompt(self, prompt_env):
        from workspace.prompt_builder import build_system_prompt

        prompt_env["state"].set_state("sess-main", "plan_ref", ".omo/plans/state-plan.md")
        _write_summary(prompt_env["knowledge_dir"], "state-plan", method="state-method")

        prompt = build_system_prompt(session_id="sess-main")

        assert "## Knowledge Summary: state-plan" in prompt
        assert "state-method" in prompt
        assert "AGENTS-PERSONA" in prompt

    def test_absent_without_plan_ref(self, prompt_env):
        from workspace.prompt_builder import build_system_prompt

        prompt = build_system_prompt(session_id="sess-main")

        assert "Knowledge Summary" not in prompt

    def test_absent_when_files_filtered(self, prompt_env):
        from workspace.prompt_builder import build_system_prompt

        prompt_env["state"].set_state("sess-main", "plan_ref", ".omo/plans/state-plan.md")
        _write_summary(prompt_env["knowledge_dir"], "state-plan", method="state-method")

        prompt = build_system_prompt(selected_file_names=["AGENTS.md"], session_id="sess-main")

        assert "Knowledge Summary" not in prompt
        assert "AGENTS-PERSONA" in prompt


class TestKnowledgePlanRefPathForms:
    """Only the plan's stem matters, so both path generations resolve."""

    def test_session_scoped_plan_ref(self, prompt_env):
        from workspace.prompt_builder import _build_knowledge_block

        prompt_env["state"].set_state(
            "sess-main", "plan_ref", "workspace/sessions/sess-main/plans/scoped-plan.md"
        )
        _write_summary(prompt_env["knowledge_dir"], "scoped-plan", method="scoped-method")

        block = _build_knowledge_block("sess-main")

        assert "Knowledge Summary: scoped-plan" in block
        assert "scoped-method" in block

    def test_bare_filename_plan_ref(self, prompt_env):
        from workspace.prompt_builder import _build_knowledge_block

        prompt_env["state"].set_state("sess-main", "plan_ref", "scoped-plan.md")
        _write_summary(prompt_env["knowledge_dir"], "scoped-plan", method="scoped-method")

        block = _build_knowledge_block("sess-main")

        assert "Knowledge Summary: scoped-plan" in block
        assert "scoped-method" in block
