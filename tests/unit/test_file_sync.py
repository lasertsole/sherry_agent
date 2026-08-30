"""Unit tests for workspace/file_sync.py — lazy copying of workspace system files."""

import logging

import pytest


@pytest.fixture
def file_sync_isolation(tmp_path, monkeypatch):
    """Isolate workspace + template dirs and the file-name list used by file_sync."""
    file_names = ["AGENTS.md", "SOUL.md", "IDENTITY.md", "USER.md"]

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


def test_existing_files_are_not_overwritten(file_sync_isolation):
    from workspace.file_sync import ensure_workspace_system_files

    ctx = file_sync_isolation
    user_file = ctx["ws"] / "AGENTS.md"
    user_file.write_text("USER-CUSTOMIZED", encoding="utf-8")

    copied = ensure_workspace_system_files()

    # AGENTS.md already exists -> not copied; the others are.
    assert "AGENTS.md" not in copied
    assert user_file.read_text(encoding="utf-8") == "USER-CUSTOMIZED"
    for name in ("SOUL.md", "IDENTITY.md", "USER.md"):
        assert (ctx["ws"] / name).exists()


def test_idempotent_second_call_copies_nothing(file_sync_isolation):
    from workspace.file_sync import ensure_workspace_system_files

    ensure_workspace_system_files()
    second = ensure_workspace_system_files()

    assert second == []


def test_missing_template_logs_warning_and_continues(file_sync_isolation, caplog):
    from workspace.file_sync import ensure_workspace_system_files

    ctx = file_sync_isolation
    (ctx["template_en"] / "AGENTS.md").unlink()

    with caplog.at_level(logging.WARNING):
        copied = ensure_workspace_system_files()

    # AGENTS.md template missing -> skipped, others still copied.
    assert "AGENTS.md" not in copied
    assert set(copied) == {"SOUL.md", "IDENTITY.md", "USER.md"}
    assert any("missing and no template" in r.getMessage() for r in caplog.records)


def test_returns_empty_when_nothing_required(file_sync_isolation):
    from workspace.file_sync import ensure_workspace_system_files

    ctx = file_sync_isolation
    for name in ctx["names"]:
        (ctx["ws"] / name).write_text("existing", encoding="utf-8")

    assert ensure_workspace_system_files() == []
