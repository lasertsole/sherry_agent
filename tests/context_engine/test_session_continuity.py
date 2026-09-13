"""Unit tests for LT-8 cross-session intent continuity.

On session end ``clear_session()`` persists the last AI reply plus the
session's active TaskFlow ids into
``src/data/session_continuity/{key}.json``; on the next session start
``build_continuity_prompt()`` injects that end state into the system prompt so
the agent can proactively continue unfinished work. Every public function is
fail-open: any failure yields ``None`` / ``""`` / ``[]`` and may never break
the caller (session cleanup or prompt assembly).
"""

import json

import pytest

pytestmark = [pytest.mark.unit, pytest.mark.timeout(60)]


# ---------------------------------------------------------------------------
# Isolation fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def continuity_dir(tmp_path, monkeypatch):
    """Point the module-level continuity dir at a per-test tmp directory."""
    from context_engine import session_continuity

    directory = tmp_path / "session_continuity"
    monkeypatch.setattr(session_continuity, "_CONTINUITY_DIR", directory)
    return directory


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
    """Stand-in for TieredMemoryStore (empty listing -> no FACTS block)."""

    def get_facts_listing(self) -> str:
        return ""


@pytest.fixture
def prompt_env(tmp_path, monkeypatch):
    """Isolate persona files, memory, todos, boulder, taskflow and continuity."""
    file_names = ["AGENTS.md"]
    (tmp_path / "AGENTS.md").write_text("AGENTS-PERSONA", encoding="utf-8")

    monkeypatch.setattr("workspace.prompt_builder.WORKSPACE_DIR", tmp_path)
    monkeypatch.setattr("workspace.prompt_builder.ALL_SYSTEM_FILE_NAMES", file_names)
    monkeypatch.setattr("workspace.prompt_builder.get_skills_text", lambda *a, **k: "SKILLS-BLOCK")
    monkeypatch.setattr("runtime.state_register_db", FakeStateDB())
    monkeypatch.setattr("agent.tools.memory.memory_store", FakeMemoryStore())
    monkeypatch.setattr("agent.tools.memory_tiered.get_tiered_store", lambda: FakeTieredStore())
    monkeypatch.setattr("workspace.prompt_builder._BOULDER_PATH", tmp_path / "boulder.json")
    monkeypatch.setattr(
        "agent.tools.todolist.registry.store_sqlite.get_todos_sync", lambda session_id: []
    )
    monkeypatch.setattr(
        "agent.tools.taskflow.registry.store_sqlite.get_active_flows_sync", lambda: []
    )
    return tmp_path


# ---------------------------------------------------------------------------
# Tests (plan §6)
# ---------------------------------------------------------------------------


class TestStateStorage:
    def test_save_and_get_session_state(self, continuity_dir):
        from context_engine import session_continuity

        session_continuity.save_session_end_state(
            session_id="sess-old",
            channel_id="chan1",
            chat_id="chat1",
            summary="I've deployed the fix and verified the tests pass.",
            taskflow_ids=["deploy-v2"],
        )

        state = session_continuity.get_last_session_state("chan1", "chat1")

        assert state is not None
        assert state["last_session_id"] == "sess-old"
        assert state["summary"] == "I've deployed the fix and verified the tests pass."
        assert state["taskflow_ids"] == ["deploy-v2"]
        assert state["ended_at"]
        assert state["ended_ts"] > 0
        # channel:chat key -> safe filename (colon replaced).
        assert (continuity_dir / "chan1_chat1.json").exists()

    def test_get_nonexistent_state(self, continuity_dir):
        from context_engine import session_continuity

        assert session_continuity.get_last_session_state("no-chan", "no-chat") is None


class TestContinuityPrompt:
    @pytest.fixture(autouse=True)
    def _channel_lookup(self, continuity_dir, monkeypatch):
        """Simulate the relation_register lookup resolving a channel session."""
        from context_engine import session_continuity

        monkeypatch.setattr(
            session_continuity,
            "_get_channel_chat_for_session",
            lambda session_id: ("chan1", "chat1"),
        )

    def test_continuity_prompt_with_state(self):
        from context_engine import session_continuity

        session_continuity.save_session_end_state(
            "sess-old", "chan1", "chat1", "deployed the fix and verified tests", []
        )

        prompt = session_continuity.build_continuity_prompt("sess-new")

        assert "## Last Session (continuity)" in prompt
        assert "deployed the fix and verified tests" in prompt

    def test_continuity_prompt_no_state(self):
        from context_engine import session_continuity

        assert session_continuity.build_continuity_prompt("sess-new") == ""

    def test_continuity_prompt_skips_self(self):
        from context_engine import session_continuity

        session_continuity.save_session_end_state(
            "sess-same", "chan1", "chat1", "my own last reply", []
        )

        assert session_continuity.build_continuity_prompt("sess-same") == ""

    def test_continuity_prompt_truncates_summary(self):
        from context_engine import session_continuity

        long_summary = "x" * 700
        session_continuity.save_session_end_state("sess-old", "chan1", "chat1", long_summary, [])

        prompt = session_continuity.build_continuity_prompt("sess-new")

        assert "x" * 500 in prompt
        assert "x" * 501 not in prompt

    def test_continuity_prompt_lists_taskflows(self):
        from context_engine import session_continuity

        session_continuity.save_session_end_state(
            "sess-old", "chan1", "chat1", "done for now", ["deploy-v2", "migrate-db"]
        )

        prompt = session_continuity.build_continuity_prompt("sess-new")

        assert "Related tasks: deploy-v2, migrate-db" in prompt

    def test_continuity_prompt_failure_safe(self, continuity_dir, monkeypatch):
        from context_engine import session_continuity

        def _boom(session_id):
            raise RuntimeError("relation_register unavailable")

        monkeypatch.setattr(session_continuity, "_get_channel_chat_for_session", _boom)

        assert session_continuity.build_continuity_prompt("sess-new") == ""


class TestClearSessionSave:
    @pytest.mark.asyncio
    async def test_clear_session_saves_state(self, continuity_dir, tmp_path, monkeypatch):
        """clear_session must persist the end state BEFORE deleting messages."""
        from context_engine import session_continuity
        from server.DAO import messages as dao

        calls: list[str] = []

        def _fake_get_messages(session_id, last_n=5):
            calls.append("get")
            return [
                {"role": "human", "content": "please continue"},
                {"role": "ai", "content": "LT8-CLEARED-SUMMARY"},
            ]

        def _fake_delete(session_id):
            calls.append("delete")
            return 0

        async def _noop_delete_thread_history(session_id):
            return None

        monkeypatch.setattr(
            "context_engine.store.core.get_messages_by_lastest_n_turns", _fake_get_messages
        )
        monkeypatch.setattr("context_engine.delete_messages_by_session", _fake_delete)
        monkeypatch.setattr(dao, "delete_thread_history", _noop_delete_thread_history)
        monkeypatch.setattr(dao, "clear_all_register_sessions", lambda *a, **k: None)
        monkeypatch.setattr(dao, "SESSIONS_DIR", tmp_path / "sessions")
        monkeypatch.setattr(
            session_continuity, "_get_active_taskflow_ids_sync", lambda session_id: ["flow-clear"]
        )

        await dao.clear_session("sess-clear")

        # Save ran BEFORE the message deletion.
        assert calls == ["get", "delete"]

        # No channel/chat registered -> session_id fallback key.
        path = continuity_dir / "sess-clear.json"
        assert path.exists()
        state = json.loads(path.read_text(encoding="utf-8"))
        assert state["last_session_id"] == "sess-clear"
        assert state["summary"] == "LT8-CLEARED-SUMMARY"
        assert state["taskflow_ids"] == ["flow-clear"]


class TestPromptBuilderInjection:
    def test_prompt_builder_injects_continuity(self, prompt_env, continuity_dir, monkeypatch):
        """The end-to-end path: saved state -> continuity block in the prompt."""
        from context_engine import session_continuity
        from workspace.prompt_builder import build_system_prompt

        session_continuity.save_session_end_state(
            "sess-old", "chan-pb", "chat-pb", "previous work was mid-deploy", ["deploy-v2"]
        )
        monkeypatch.setattr(
            session_continuity,
            "_get_channel_chat_for_session",
            lambda session_id: ("chan-pb", "chat-pb"),
        )

        prompt = build_system_prompt(session_id="sess-new")

        assert "## Last Session (continuity)" in prompt
        assert "previous work was mid-deploy" in prompt
        assert "Related tasks: deploy-v2" in prompt
        assert "AGENTS-PERSONA" in prompt
