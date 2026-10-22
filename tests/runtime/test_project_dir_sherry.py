"""``maybe_create_sherry_dir``: lazy, explicitly-bound project directory only.

The unified ``.sherry/`` shell is created on the first message of a session
whose project root the USER bound — never for a process default, never at
binding time, and never twice. A missing project ``FACTS.md`` is seeded from
the language template; an existing file is never overwritten.
"""

from __future__ import annotations

import pytest

from runtime.session import state_register_mem
from runtime.session.project_dir import maybe_create_sherry_dir
from runtime.session.state_keys import StateKey

pytestmark = [pytest.mark.unit]

SESSION = "sess-sherry-dir"


@pytest.fixture
def project(tmp_path, monkeypatch):
    """A bound project root + a deterministic FACTS template."""
    root = tmp_path / "project"
    root.mkdir()
    template_dir = tmp_path / "template"
    template_dir.mkdir()
    (template_dir / "FACTS.md").write_text("TEMPLATE FACTS", encoding="utf-8")
    monkeypatch.setattr(
        "config.path.resolve_workspace_template_dir", lambda lang=None: template_dir
    )
    state_register_mem.set_state(SESSION, StateKey.PROJECT_DIR, str(root))
    yield root
    state_register_mem.clear_session(SESSION)


def test_a_bound_project_gets_the_shell_and_a_seeded_facts_file(project):
    assert maybe_create_sherry_dir(SESSION) is True

    assert (project / ".sherry" / "memory" / "FACTS.md").read_text(encoding="utf-8") == (
        "TEMPLATE FACTS"
    )


def test_a_second_call_is_idempotent_and_never_overwrites(project):
    assert maybe_create_sherry_dir(SESSION) is True
    facts = project / ".sherry" / "memory" / "FACTS.md"
    facts.write_text("user-authored", encoding="utf-8")

    assert maybe_create_sherry_dir(SESSION) is False
    assert facts.read_text(encoding="utf-8") == "user-authored"


def test_an_existing_shell_without_facts_gets_the_template(project):
    memory = project / ".sherry" / "memory"
    memory.mkdir(parents=True)

    assert maybe_create_sherry_dir(SESSION) is False
    assert (memory / "FACTS.md").read_text(encoding="utf-8") == "TEMPLATE FACTS"


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
