"""Unit tests for workspace/prompt_builder.py — system prompt assembly & workspace caching.

The static workspace file content is cached per ``session_id`` under the
state_register_db key ``workspace``. Once a session's cache is written it must
NEVER change for that session, so that mid-conversation edits to workspace
files do not alter an in-flight session's persona.

Dynamic content (memory_store) is intentionally NOT cached.
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
        sess = self._states.get(session_id, {})
        return sess.get(key, default)

    def set_state(self, session_id, key, value):
        self._states.setdefault(session_id, {})[key] = value
        return True


class FakeMemoryStore:
    """Stand-in for agent.tools.memory.memory_store."""

    def __init__(self, memory: str = "MEMORY-V1", user: str = "USER-V1", facts: str | None = None):
        self.memory = memory
        self.user = user
        self.facts = facts

    def format_for_system_prompt(self, target: str):
        if target == "memory":
            return self.memory
        if target == "user":
            return self.user
        if target == "facts":
            return self.facts
        return None


pytestmark = [pytest.mark.unit]


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    """Isolated workspace dir + fixed file names + fake dependencies."""
    # Fixed set of "system" files that the no-args path reads.
    file_names = ["AGENTS.md", "SOUL.md"]
    (tmp_path / "AGENTS.md").write_text("AGENTS-v1", encoding="utf-8")
    (tmp_path / "SOUL.md").write_text("SOUL-v1", encoding="utf-8")

    monkeypatch.setattr("workspace.prompt_builder.WORKSPACE_DIR", tmp_path)
    monkeypatch.setattr("workspace.prompt_builder.ALL_SYSTEM_FILE_NAMES", file_names)
    monkeypatch.setattr("workspace.prompt_builder.get_skills_text", lambda *a, **k: "SKILLS-BLOCK")
    monkeypatch.setattr("runtime.state_register_db", FakeStateDB())

    fakes = {
        "dir": tmp_path,
        "file_names": file_names,
    }
    return fakes


def _set_memory_store(monkeypatch, *, memory="MEMORY-V1", user="USER-V1", facts=None):
    """Patch prompt_builder's lazy memory_store import target."""
    fake_mem = FakeMemoryStore(memory=memory, user=user, facts=facts)
    monkeypatch.setattr("agent.tools.memory.memory_store", fake_mem)
    return fake_mem


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestSameSessionPermanent:
    """A session's cached persona snapshot is frozen once created."""

    def test_same_session_ignores_later_file_edits(self, workspace, monkeypatch):
        from workspace.prompt_builder import build_system_prompt

        _set_memory_store(monkeypatch)

        first = build_system_prompt(session_id="sess-1")

        # Drastically change the underlying workspace files AFTER first build.
        (workspace["dir"] / "AGENTS.md").write_text("AGENTS-VERSION-999-CHANGED", encoding="utf-8")
        (workspace["dir"] / "SOUL.md").write_text("SOUL-VERSION-999-CHANGED", encoding="utf-8")

        second = build_system_prompt(session_id="sess-1")

        assert first == second, "same session must keep its original persona snapshot"

    def test_same_session_frozen_across_many_calls(self, workspace, monkeypatch):
        from workspace.prompt_builder import build_system_prompt

        _set_memory_store(monkeypatch)

        baseline = build_system_prompt(session_id="sess-2")
        for _ in range(5):
            (workspace["dir"] / "AGENTS.md").write_text(f"AGENTS-iter-{_}", encoding="utf-8")
            assert build_system_prompt(session_id="sess-2") == baseline


class TestNewSessionFresh:
    """A brand-new session reads the latest workspace content."""

    def test_new_session_after_edit_gets_new_content(self, workspace, monkeypatch):
        from workspace.prompt_builder import build_system_prompt

        _set_memory_store(monkeypatch)

        old = build_system_prompt(session_id="sess-old")

        (workspace["dir"] / "AGENTS.md").write_text("AGENTS-BRAND-NEW", encoding="utf-8")
        (workspace["dir"] / "SOUL.md").write_text("SOUL-BRAND-NEW", encoding="utf-8")

        fresh = build_system_prompt(session_id="sess-brand-new")

        assert self._has_content(fresh, "AGENTS-BRAND-NEW")
        assert self._has_content(fresh, "SOUL-BRAND-NEW")
        assert fresh != old

    @staticmethod
    def _has_content(prompt: str, needle: str) -> bool:
        return needle in prompt


class TestSessionIsolation:
    """Caches of different sessions are fully independent."""

    def test_sessions_do_not_share_cache(self, workspace, monkeypatch):
        from workspace.prompt_builder import build_system_prompt

        _set_memory_store(monkeypatch)

        a1 = build_system_prompt(session_id="sess-A")

        (workspace["dir"] / "AGENTS.md").write_text("AGENTS-A-ONLY-NEW", encoding="utf-8")

        # A's snapshot is frozen (already cached before the edit).
        # B is a NEW session created AFTER the edit -> it reads fresh content.
        a2 = build_system_prompt(session_id="sess-A")
        b2 = build_system_prompt(session_id="sess-B")

        assert a1 == a2
        assert self._has_content(b2, "AGENTS-A-ONLY-NEW")

    @staticmethod
    def _has_content(prompt: str, needle: str) -> bool:
        return needle in prompt


class TestNoSessionNoCache:
    """Without session_id the static files are re-read every time."""

    def test_no_session_always_refreshes(self, workspace, monkeypatch):
        from workspace.prompt_builder import build_system_prompt

        _set_memory_store(monkeypatch)

        build_system_prompt()

        (workspace["dir"] / "AGENTS.md").write_text("AGENTS-NO-SESSION-NEW", encoding="utf-8")

        refreshed = build_system_prompt()
        assert "AGENTS-NO-SESSION-NEW" in refreshed

    def test_empty_string_session_no_cache(self, workspace, monkeypatch):
        from workspace.prompt_builder import build_system_prompt

        _set_memory_store(monkeypatch)

        build_system_prompt(session_id="")

        (workspace["dir"] / "AGENTS.md").write_text("AGENTS-EMPTY-STR-NEW", encoding="utf-8")

        assert "AGENTS-EMPTY-STR-NEW" in build_system_prompt(session_id="")


class TestSelectedFilesCaching:
    """Explicit selected_file_names branch is also cached per session."""

    def test_explicit_files_cached_and_frozen(self, workspace, monkeypatch):
        from workspace.prompt_builder import build_system_prompt

        _set_memory_store(monkeypatch)

        first = build_system_prompt(selected_file_names=["AGENTS.md"], session_id="sess-sel")

        (workspace["dir"] / "AGENTS.md").write_text("AGENTS-EXPLICIT-CHANGED", encoding="utf-8")

        second = build_system_prompt(selected_file_names=["AGENTS.md"], session_id="sess-sel")
        assert first == second


class TestMemoryNotCached:
    """Dynamic memory_store content must stay fresh, never frozen in cache."""

    def test_memory_updates_live_in_same_session(self, workspace, monkeypatch):
        from workspace.prompt_builder import build_system_prompt

        fake_mem = _set_memory_store(monkeypatch, memory="MEM-V1", user="USER-V1")

        build_system_prompt(session_id="sess-mem")

        # memory changes + workspace changes after first build
        fake_mem.memory = "MEM-V2-NEW"
        fake_mem.user = "USER-V2-NEW"
        (workspace["dir"] / "AGENTS.md").write_text("AGENTS-MEM-CHANGED", encoding="utf-8")

        rebuilt = build_system_prompt(session_id="sess-mem")

        # Frozen persona + live memory
        assert "AGENTS-v1" in rebuilt, "persona snapshot must remain frozen"
        assert "MEM-V2-NEW" in rebuilt, "memory must be live"
        assert "USER-V2-NEW" in rebuilt, "user memory must be live"
        assert "MEM-V1" not in rebuilt, "old memory should not appear"

    def test_facts_block_injected_and_empty_skipped(self, workspace, monkeypatch):
        from workspace.prompt_builder import build_system_prompt

        fake_mem = _set_memory_store(monkeypatch, facts="FACTS-V1")

        with_facts = build_system_prompt(session_id="sess-facts")
        assert "FACTS-V1" in with_facts

        fake_mem.facts = None
        without_facts = build_system_prompt(session_id="sess-facts-empty")
        assert "FACTS-V1" not in without_facts


class TestSessionSkillSelection:
    """The session's 预设-技能 selection drives the ``<available_skills>`` index.

    The seam under test: ``build_system_prompt`` must hand the session's own
    names to ``get_skills_text`` with ``exact=True`` (an explicit empty list
    selects NO skill), while a session without a selection keeps the
    every-skill behaviour.
    """

    @staticmethod
    def _recorder(monkeypatch):
        calls: list[dict] = []

        def fake_get_skills_text(selected_skill_names=None, caller_scope="main", **kwargs):
            calls.append(
                {
                    "names": selected_skill_names,
                    "caller_scope": caller_scope,
                    **kwargs,
                }
            )
            return "SKILLS-BLOCK"

        monkeypatch.setattr("workspace.prompt_builder.get_skills_text", fake_get_skills_text)
        return calls

    def test_no_selection_means_every_skill(self, workspace, monkeypatch):
        from workspace.prompt_builder import build_system_prompt

        calls = self._recorder(monkeypatch)

        build_system_prompt(session_id="sess-skills-none")

        assert calls[-1]["names"] is None
        assert calls[-1]["exact"] is False

    def test_the_session_selection_is_read_from_the_register(self, workspace, monkeypatch):
        from runtime.session.state_register import state_register_mem
        from runtime.session.state_keys import StateKey
        from skills.loader import REQUIRED_SKILLS
        from workspace.prompt_builder import build_system_prompt

        calls = self._recorder(monkeypatch)
        state_register_mem.set_state(
            "sess-skills-picked", StateKey.AGENT_CONFIG, {"skills": ["alpha", "beta"]}
        )

        build_system_prompt(session_id="sess-skills-picked")

        assert calls[-1]["names"] == sorted({"alpha", "beta", *REQUIRED_SKILLS})
        assert calls[-1]["exact"] is True

    def test_an_empty_selection_is_exact(self, workspace, monkeypatch):
        """Unchecking everything must NOT fall back to every skill — the media
        chain is all that remains (the legal minimum)."""
        from runtime.session.state_register import state_register_mem
        from runtime.session.state_keys import StateKey
        from skills.loader import REQUIRED_SKILLS
        from workspace.prompt_builder import build_system_prompt

        calls = self._recorder(monkeypatch)
        state_register_mem.set_state("sess-skills-empty", StateKey.AGENT_CONFIG, {"skills": []})

        build_system_prompt(session_id="sess-skills-empty")

        assert calls[-1]["names"] == sorted(REQUIRED_SKILLS)
        assert calls[-1]["exact"] is True

    def test_an_explicit_argument_outranks_the_session(self, workspace, monkeypatch):
        from runtime.session.state_register import state_register_mem
        from runtime.session.state_keys import StateKey
        from skills.loader import REQUIRED_SKILLS
        from workspace.prompt_builder import build_system_prompt

        calls = self._recorder(monkeypatch)
        state_register_mem.set_state(
            "sess-skills-arg", StateKey.AGENT_CONFIG, {"skills": ["alpha"]}
        )

        build_system_prompt(selected_skill_names=["gamma"], session_id="sess-skills-arg")

        assert calls[-1]["names"] == sorted({"gamma", *REQUIRED_SKILLS})

    def test_the_required_multimedia_skills_are_unioned_back_in(self, workspace, monkeypatch):
        """A session config cannot drop the media chain (image/audio/video/text_to_image)."""
        from runtime.session.state_register import state_register_mem
        from runtime.session.state_keys import StateKey
        from skills.loader import REQUIRED_SKILLS
        from workspace.prompt_builder import build_system_prompt

        calls = self._recorder(monkeypatch)
        state_register_mem.set_state(
            "sess-skills-req", StateKey.AGENT_CONFIG, {"skills": ["alpha"]}
        )

        build_system_prompt(session_id="sess-skills-req")

        assert calls[-1]["names"] == sorted({"alpha", *REQUIRED_SKILLS})
        assert calls[-1]["exact"] is True

    def test_a_caller_without_a_session_stays_unfiltered(self, workspace, monkeypatch):
        from workspace.prompt_builder import build_system_prompt

        calls = self._recorder(monkeypatch)

        build_system_prompt()

        assert calls[-1]["names"] is None
