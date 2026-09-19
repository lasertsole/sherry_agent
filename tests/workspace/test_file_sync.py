"""Unit tests for workspace/file_sync.py — lazy copying of workspace system files.

Root system files are copied to ``workspace/``; memory system files (FACTS.md)
are copied to ``workspace/memory/``. An empty memory file counts as missing so
the template's purpose comment can land after a first-boot touch; authored
content is never overwritten.
"""

import logging

import pytest


pytestmark = [pytest.mark.unit]


@pytest.fixture
def file_sync_isolation(tmp_path, monkeypatch):
    """Isolate workspace + template dirs and the file-name lists used by file_sync."""
    file_names = ["AGENTS.md", "SOUL.md", "USER.md"]
    memory_names = ["FACTS.md"]

    ws = tmp_path / "workspace"
    ws.mkdir()
    memory_dir = ws / "memory"
    template_dir = tmp_path / "template"
    template_dir.mkdir()
    template_en = template_dir / "en"
    template_en.mkdir()
    for name in [*file_names, *memory_names]:
        (template_en / name).write_text(f"TEMPLATE-{name}", encoding="utf-8")

    import workspace.file_sync as mod

    monkeypatch.setattr(mod, "WORKSPACE_DIR", ws)
    monkeypatch.setattr(mod, "MEMORY_DIR", memory_dir)
    monkeypatch.setattr(mod, "ALL_SYSTEM_FILE_NAMES", file_names)
    monkeypatch.setattr(mod, "MEMORY_SYSTEM_FILE_NAMES", memory_names)
    monkeypatch.setattr(
        mod,
        "resolve_workspace_template_dir",
        lambda lang=None: template_en,
    )

    return {
        "ws": ws,
        "memory_dir": memory_dir,
        "template_en": template_en,
        "names": file_names,
        "memory_names": memory_names,
    }


def _expected_copied(ctx) -> set[str]:
    return set(ctx["names"]) | {f"memory/{name}" for name in ctx["memory_names"]}


def test_copies_all_missing_files(file_sync_isolation):
    from workspace.file_sync import ensure_workspace_system_files

    ctx = file_sync_isolation
    copied = ensure_workspace_system_files()

    assert set(copied) == _expected_copied(ctx)
    for name in ctx["names"]:
        target = ctx["ws"] / name
        assert target.exists()
        assert target.read_text(encoding="utf-8") == f"TEMPLATE-{name}"
    for name in ctx["memory_names"]:
        target = ctx["memory_dir"] / name
        assert target.exists()
        assert target.read_text(encoding="utf-8") == f"TEMPLATE-{name}"


def test_existing_files_are_not_overwritten(file_sync_isolation):
    from workspace.file_sync import ensure_workspace_system_files

    ctx = file_sync_isolation
    user_file = ctx["ws"] / "AGENTS.md"
    user_file.write_text("USER-CUSTOMIZED", encoding="utf-8")
    facts_file = ctx["memory_dir"] / "FACTS.md"
    facts_file.parent.mkdir(parents=True, exist_ok=True)
    facts_file.write_text("AUTHORED-FACT", encoding="utf-8")

    copied = ensure_workspace_system_files()

    # AGENTS.md and FACTS.md already exist -> not copied; the others are.
    assert "AGENTS.md" not in copied
    assert "memory/FACTS.md" not in copied
    assert user_file.read_text(encoding="utf-8") == "USER-CUSTOMIZED"
    assert facts_file.read_text(encoding="utf-8") == "AUTHORED-FACT"
    for name in ("SOUL.md", "USER.md"):
        assert (ctx["ws"] / name).exists()


def test_empty_memory_file_gets_template(file_sync_isolation):
    """A first-boot ``touch()`` leaves an empty file; the template comment lands."""
    from workspace.file_sync import ensure_workspace_system_files

    ctx = file_sync_isolation
    facts_file = ctx["memory_dir"] / "FACTS.md"
    facts_file.parent.mkdir(parents=True, exist_ok=True)
    facts_file.touch()

    copied = ensure_workspace_system_files()

    assert "memory/FACTS.md" in copied
    assert facts_file.read_text(encoding="utf-8") == "TEMPLATE-FACTS.md"


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
    assert set(copied) == _expected_copied(ctx) - {"AGENTS.md"}
    assert any("missing and no template" in r.getMessage() for r in caplog.records)


def test_returns_empty_when_nothing_required(file_sync_isolation):
    from workspace.file_sync import ensure_workspace_system_files

    ctx = file_sync_isolation
    for name in ctx["names"]:
        (ctx["ws"] / name).write_text("existing", encoding="utf-8")
    for name in ctx["memory_names"]:
        target = ctx["memory_dir"] / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("existing", encoding="utf-8")

    assert ensure_workspace_system_files() == []
