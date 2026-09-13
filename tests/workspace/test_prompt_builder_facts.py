"""Unit tests for the LT-1 FACTS listing injection in workspace/prompt_builder.py.

The tiered facts store is on-demand (fact_read/fact_search), so the system
prompt only carries a one-line listing of the non-empty category files. The
injection must fail-open: a broken facts layer must never prevent the persona
prompt from being assembled.

The listing is only injected in the unfiltered branch (``selected_file_names``
is None), matching the dynamic memory block rule.
"""

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


class FakeTieredStore:
    """Stand-in for TieredMemoryStore with a controllable listing."""

    def __init__(self, listing: str = "", error: Exception | None = None):
        self.listing = listing
        self.error = error
        self.calls = 0

    def get_facts_listing(self) -> str:
        self.calls += 1
        if self.error is not None:
            raise self.error
        return self.listing


pytestmark = [pytest.mark.unit]


@pytest.fixture
def prompt_env(tmp_path, monkeypatch):
    """Isolate persona files, memory, todos, boulder and the facts layer."""
    file_names = ["AGENTS.md", "SOUL.md"]
    (tmp_path / "AGENTS.md").write_text("AGENTS-PERSONA", encoding="utf-8")
    (tmp_path / "SOUL.md").write_text("SOUL-PERSONA", encoding="utf-8")

    monkeypatch.setattr("workspace.prompt_builder.WORKSPACE_DIR", tmp_path)
    monkeypatch.setattr("workspace.prompt_builder.ALL_SYSTEM_FILE_NAMES", file_names)
    monkeypatch.setattr("workspace.prompt_builder.get_skills_text", lambda *a, **k: "SKILLS-BLOCK")
    monkeypatch.setattr("runtime.state_register_db", FakeStateDB())
    monkeypatch.setattr("agent.tools.memory.memory_store", FakeMemoryStore())

    # No boulder file and no todos -> those blocks stay empty.
    monkeypatch.setattr("workspace.prompt_builder._BOULDER_PATH", tmp_path / "boulder.json")
    monkeypatch.setattr(
        "agent.tools.todolist.registry.store_sqlite.get_todos_sync", lambda session_id: []
    )
    # No active taskflows -> the taskflow block stays empty.
    monkeypatch.setattr(
        "agent.tools.taskflow.registry.store_sqlite.get_active_flows_sync", lambda: []
    )

    state: dict[str, object] = {"tiered": FakeTieredStore(), "factory_calls": 0}

    def fake_get_tiered_store():
        state["factory_calls"] = int(state["factory_calls"]) + 1  # type: ignore[arg-type]
        tiered = state["tiered"]
        assert isinstance(tiered, FakeTieredStore)
        return tiered

    monkeypatch.setattr("agent.tools.memory_tiered.get_tiered_store", fake_get_tiered_store)

    return {"state": state}


def _tiered(prompt_env) -> FakeTieredStore:
    tiered = prompt_env["state"]["tiered"]
    assert isinstance(tiered, FakeTieredStore)
    return tiered


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestFactsListingInjection:
    def test_facts_listing_injected_when_non_empty(self, prompt_env):
        from workspace.prompt_builder import build_system_prompt

        _tiered(prompt_env).listing = "environment (2 entries) · project (1 entries)"

        prompt = build_system_prompt(session_id="sess-facts")

        assert "FACTS (on-demand" in prompt
        assert "environment (2 entries) · project (1 entries)" in prompt
        assert "fact_read/fact_search" in prompt
        assert "AGENTS-PERSONA" in prompt

    def test_facts_listing_absent_when_empty(self, prompt_env):
        from workspace.prompt_builder import build_system_prompt

        _tiered(prompt_env).listing = ""

        prompt = build_system_prompt(session_id="sess-facts-empty")

        assert "FACTS (on-demand" not in prompt
        assert "AGENTS-PERSONA" in prompt


class TestFactsFailOpen:
    def test_facts_error_still_builds_prompt(self, prompt_env):
        from workspace.prompt_builder import build_system_prompt

        _tiered(prompt_env).error = RuntimeError("facts layer exploded")

        prompt = build_system_prompt(session_id="sess-facts-broken")

        assert "FACTS (on-demand" not in prompt
        assert "AGENTS-PERSONA" in prompt
        assert "SKILLS-BLOCK" in prompt


class TestFactsFilterGuard:
    def test_filtered_files_skip_facts_listing(self, prompt_env):
        from workspace.prompt_builder import build_system_prompt

        _tiered(prompt_env).listing = "environment (2 entries)"

        prompt = build_system_prompt(selected_file_names=["AGENTS.md"], session_id="sess-facts-sel")

        assert "FACTS (on-demand" not in prompt
        assert prompt_env["state"]["factory_calls"] == 0
        assert "AGENTS-PERSONA" in prompt
