"""Unit tests for workspace/file_sync.py — lazy copying of workspace system files.

Persona files are copied to ``workspace/`` from the language template on first
use. Existing files are never overwritten. Long-term memory is SQLite, so no
memory files are seeded.
"""

import pytest
from loguru import logger


pytestmark = [pytest.mark.unit]


@pytest.fixture
def file_sync_isolation(tmp_path, monkeypatch):
    """Isolate the workspace + template dirs and the file-name list."""
    file_names = ["AGENTS.md", "SOUL.md", "USER.md"]

    ws = tmp_path / "workspace"
    ws.mkdir()
    template_dir = tmp_path / "template"
    template_dir.mkdir()
    template_en = template_dir / "en"
    template_en.mkdir()
    for name in file_names:
        (template_en / name).write_text(f"TEMPLATE-{name}", encoding="utf-8")

    import workspace.file_sync as mod

    monkeypatch.setattr(mod, "WORKSPACE_DIR", ws)
    monkeypatch.setattr(mod, "ALL_SYSTEM_FILE_NAMES", file_names)
    monkeypatch.setattr(
        mod,
        "resolve_workspace_template_dir",
        lambda lang=None: template_en,
    )

    return {"ws": ws, "template_en": template_en, "names": file_names}


def test_copies_all_missing_files(file_sync_isolation):
    from workspace.file_sync import ensure_workspace_system_files

    ctx = file_sync_isolation
    copied = ensure_workspace_system_files()

    assert set(copied) == set(ctx["names"])
    for name in ctx["names"]:
        target = ctx["ws"] / name
        assert target.exists()
        assert target.read_text(encoding="utf-8") == f"TEMPLATE-{name}"
    assert not (ctx["ws"] / "memory").exists()


def test_existing_files_are_not_overwritten(file_sync_isolation):
    from workspace.file_sync import ensure_workspace_system_files

    ctx = file_sync_isolation
    user_file = ctx["ws"] / "AGENTS.md"
    user_file.write_text("USER-CUSTOMIZED", encoding="utf-8")

    copied = ensure_workspace_system_files()

    assert "AGENTS.md" not in copied
    assert user_file.read_text(encoding="utf-8") == "USER-CUSTOMIZED"
    for name in ("SOUL.md", "USER.md"):
        assert (ctx["ws"] / name).exists()


def test_idempotent_second_call_copies_nothing(file_sync_isolation):
    from workspace.file_sync import ensure_workspace_system_files

    ensure_workspace_system_files()
    second = ensure_workspace_system_files()

    assert second == []


def test_missing_template_logs_warning_and_continues(file_sync_isolation):
    from workspace.file_sync import ensure_workspace_system_files

    ctx = file_sync_isolation
    (ctx["template_en"] / "AGENTS.md").unlink()

    # file_sync logs through loguru (stdlib records never reached its sinks), so
    # the warning is captured with a sink rather than caplog.
    messages: list[str] = []
    handler_id = logger.add(messages.append, level="WARNING", format="{message}")
    try:
        copied = ensure_workspace_system_files()
    finally:
        logger.remove(handler_id)

    # AGENTS.md template missing -> skipped, others still copied.
    assert "AGENTS.md" not in copied
    assert set(copied) == set(ctx["names"]) - {"AGENTS.md"}
    assert any("missing and no template" in message for message in messages), messages


def test_returns_empty_when_nothing_required(file_sync_isolation):
    from workspace.file_sync import ensure_workspace_system_files

    ctx = file_sync_isolation
    for name in ctx["names"]:
        (ctx["ws"] / name).write_text("existing", encoding="utf-8")

    assert ensure_workspace_system_files() == []


def test_a_missing_workspace_root_is_created_even_with_no_templates(tmp_path, monkeypatch):
    """First run of the packaged app: the home exists, workspace/ does not, and
    a write must not fail because nothing got copied."""
    import workspace.file_sync as mod

    ws = tmp_path / "fresh-home" / "workspace"
    monkeypatch.setattr(mod, "WORKSPACE_DIR", ws)
    monkeypatch.setattr(mod, "ALL_SYSTEM_FILE_NAMES", ["AGENTS.md"])
    monkeypatch.setattr(
        mod, "resolve_workspace_template_dir", lambda lang=None: tmp_path / "no-template"
    )

    assert mod.ensure_workspace_system_files() == []
    assert ws.is_dir()
