"""Persona file set invariants — the role statement file (``ROLE.md``).

The role statement ("你将扮演… / 用户将扮演…") is a persona file exactly like
SOUL.md: declared in ``ALL_SYSTEM_FILE_NAMES`` so it enters the frozen
per-session prompt snapshot, shipped as a template in every language so a fresh
checkout has a sensible default, and editable through the persona API so the
预设 panel can apply it.
"""

import pytest

pytestmark = [pytest.mark.unit]

TEMPLATE_LANGS = ("en", "zh", "ja", "ko")


def test_role_file_is_a_system_file():
    """ROLE.md is declared next to SOUL.md/USER.md (this is what puts it in the prompt)."""
    from workspace import ALL_SYSTEM_FILE_NAMES, COMMUNITY_SYSTEM_FILE_NAMES

    assert "ROLE.md" in COMMUNITY_SYSTEM_FILE_NAMES
    assert "ROLE.md" in ALL_SYSTEM_FILE_NAMES


def test_role_file_is_editable_through_the_persona_api():
    """The persona file API (``PUT /system_prompt``) accepts ROLE.md like any other persona file."""
    from server.service.workplace import EDITABLE_SYSTEM_FILE_NAMES

    assert "ROLE.md" in EDITABLE_SYSTEM_FILE_NAMES


@pytest.mark.parametrize("lang", TEMPLATE_LANGS)
def test_templates_ship_every_declared_system_file(lang):
    """Every language template directory carries every declared file (a missing ROLE.md is silent otherwise)."""
    from config.path import WORKSPACE_TEMPLATE_DIR
    from workspace import ALL_SYSTEM_FILE_NAMES, MEMORY_SYSTEM_FILE_NAMES

    template_dir = WORKSPACE_TEMPLATE_DIR / lang
    for name in (*ALL_SYSTEM_FILE_NAMES, *MEMORY_SYSTEM_FILE_NAMES):
        path = template_dir / name
        assert path.is_file(), f"{lang} template is missing {name}"
        assert path.read_text(encoding="utf-8").strip(), f"{lang}/{name} is empty"


@pytest.mark.parametrize("lang", TEMPLATE_LANGS)
def test_role_templates_state_both_roles(lang):
    """Format contract: ``# ROLE.md`` header + one AI line + one user line."""
    from config.path import WORKSPACE_TEMPLATE_DIR

    lines = [
        line
        for line in (WORKSPACE_TEMPLATE_DIR / lang / "ROLE.md")
        .read_text(encoding="utf-8")
        .splitlines()
        if line.strip()
    ]
    assert lines[0] == "# ROLE.md", f"{lang} ROLE.md header"
    assert len(lines) == 3, f"{lang} ROLE.md must hold exactly two statements, got {lines!r}"


def test_role_statement_reaches_the_system_prompt(tmp_path, monkeypatch):
    """End to end through the real declared list + real template: the sentence lands in the prompt.

    ``_read_static_files`` lazy-copies missing files from the language template
    directory first, so an empty workspace must come out with the template's role
    statement in the assembled prompt.
    """
    from config.path import WORKSPACE_TEMPLATE_DIR
    from workspace import ALL_SYSTEM_FILE_NAMES
    from workspace.prompt_builder import build_system_prompt

    class FakeMemoryStore:
        """Stand-in for agent.tools.memory.memory_store (no live reads)."""

        def format_for_system_prompt(self, target: str):
            """Return a fixed block for the memory targets."""
            return {"memory": "MEMORY-V1", "user": "USER-V1", "facts": None}.get(target)

    monkeypatch.setattr("workspace.file_sync.WORKSPACE_DIR", tmp_path)
    monkeypatch.setattr("workspace.file_sync.MEMORY_DIR", tmp_path / "memory")
    monkeypatch.setattr(
        "workspace.file_sync.resolve_workspace_template_dir",
        lambda lang=None: WORKSPACE_TEMPLATE_DIR / "zh",
    )
    monkeypatch.setattr("workspace.prompt_builder.WORKSPACE_DIR", tmp_path)
    monkeypatch.setattr(
        "workspace.prompt_builder.ALL_SYSTEM_FILE_NAMES", list(ALL_SYSTEM_FILE_NAMES)
    )
    monkeypatch.setattr("workspace.prompt_builder.get_skills_text", lambda *a, **k: "")
    monkeypatch.setattr("agent.tools.memory.memory_store", FakeMemoryStore())

    prompt = build_system_prompt()

    assert "你将扮演橘雪莉。" in prompt
    assert "用户将扮演远野汉娜。" in prompt
