"""``maybe_create_sherry_dir``: lazy, explicitly-bound project directory only.

The unified ``.sherry/`` shell is created on the first message of a session
whose project root the USER bound — never for a process default, never at
binding time, and never twice. FACTS.md is global, so this creates only the
shell and never touches a project-side memory directory.
"""

from __future__ import annotations

import pytest

from runtime.session import state_register_mem
from runtime.session.project_dir import maybe_create_sherry_dir
from runtime.session.state_keys import StateKey

pytestmark = [pytest.mark.unit]

SESSION = "sess-sherry-dir"


@pytest.fixture
def project(tmp_path):
    """A bound project root."""
    root = tmp_path / "project"
    root.mkdir()
    state_register_mem.set_state(SESSION, StateKey.PROJECT_DIR, str(root))
    yield root
    state_register_mem.clear_session(SESSION)


def test_a_bound_project_gets_the_shell(project):
    assert maybe_create_sherry_dir(SESSION) is True

    assert (project / ".sherry").is_dir()
    assert not (project / ".sherry" / "memory").exists()


def test_a_second_call_is_idempotent(project):
    assert maybe_create_sherry_dir(SESSION) is True

    assert maybe_create_sherry_dir(SESSION) is False


def test_an_existing_project_facts_file_is_left_untouched(project):
    """FACTS is global now; a legacy project file is neither read nor rewritten."""
    legacy = project / ".sherry" / "memory" / "FACTS.md"
    legacy.parent.mkdir(parents=True)
    legacy.write_text("legacy project fact", encoding="utf-8")

    assert maybe_create_sherry_dir(SESSION) is False
    assert legacy.read_text(encoding="utf-8") == "legacy project fact"


def test_an_unbound_session_creates_nothing(tmp_path, monkeypatch):
    default = tmp_path / "default"
    default.mkdir()
    monkeypatch.setattr("config.path.resolve_default_project_dir", lambda: default)

    assert maybe_create_sherry_dir("sess-unbound") is False
    assert not (default / ".sherry").exists()


def test_a_missing_project_directory_creates_nothing(project, monkeypatch, tmp_path):
    state_register_mem.set_state(SESSION, StateKey.PROJECT_DIR, str(tmp_path / "gone"))

    assert maybe_create_sherry_dir(SESSION) is False
    assert not (tmp_path / "gone").exists()
