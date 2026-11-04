"""One-shot legacy Markdown -> SQLite memory import."""

from __future__ import annotations

import pytest

import context_engine.store.memory_migration as migration
from context_engine.store.memory_entry_repository import MemoryEntryRepository

pytestmark = [pytest.mark.unit, pytest.mark.timeout(60)]


@pytest.fixture
def memory_dir(tmp_path, monkeypatch):
    directory = tmp_path / "memory"
    directory.mkdir()
    monkeypatch.setattr(migration, "MEMORY_DIR", directory)
    return directory


@pytest.fixture
def repo(tmp_path):
    repository = MemoryEntryRepository(db_path=tmp_path / "memory.db")
    yield repository
    repository.close()


def test_imports_all_three_files_and_renames_them(memory_dir, repo):
    (memory_dir / "MEMORY.md").write_text(
        "# MEMORY\n§\nEnvironment: Python 3.13\n§\nProject: uses uv",
        encoding="utf-8",
    )
    (memory_dir / "USER.md").write_text("User: likes tea", encoding="utf-8")
    (memory_dir / "FACTS.md").write_text("pitfall one\n§\npitfall two", encoding="utf-8")

    imported = migration.migrate_legacy_memory_files(repo, skip_in_tests=False)

    assert imported == 5
    assert [entry["content"] for entry in repo.active_entries_ordered("memory")] == [
        "Environment: Python 3.13",
        "Project: uses uv",
    ]
    assert [entry["content"] for entry in repo.active_entries_ordered("user")] == [
        "User: likes tea"
    ]
    assert [entry["content"] for entry in repo.active_entries_ordered("facts")] == [
        "pitfall one",
        "pitfall two",
    ]
    assert repo.active_entries_ordered("memory")[0]["entry_type"] == "project"
    assert repo.active_entries_ordered("user")[0]["entry_type"] == "user"
    assert repo.active_entries_ordered("facts")[0]["entry_type"] == "system"
    assert repo.active_entries_ordered("memory")[0]["origin_session"] is None

    for name in ("MEMORY.md", "USER.md", "FACTS.md"):
        assert not (memory_dir / name).exists()
        assert (memory_dir / f"{name}.bak").exists()


def test_is_idempotent_once_rows_exist(memory_dir, repo):
    (memory_dir / "MEMORY.md").write_text("note", encoding="utf-8")
    assert migration.migrate_legacy_memory_files(repo, skip_in_tests=False) == 1

    (memory_dir / "MEMORY.md").write_text("second boot note", encoding="utf-8")
    assert migration.migrate_legacy_memory_files(repo, skip_in_tests=False) == 0
    assert [entry["content"] for entry in repo.active_entries_ordered("memory")] == ["note"]


def test_strips_the_legacy_h1_header(memory_dir, repo):
    (memory_dir / "USER.md").write_text("# USER\n§\nUser: prefers Chinese", encoding="utf-8")

    migration.migrate_legacy_memory_files(repo, skip_in_tests=False)

    assert [entry["content"] for entry in repo.active_entries_ordered("user")] == [
        "User: prefers Chinese"
    ]


def test_deduplicates_within_a_file(memory_dir, repo):
    (memory_dir / "FACTS.md").write_text("same\n§\nsame\n§\nother", encoding="utf-8")

    migration.migrate_legacy_memory_files(repo, skip_in_tests=False)

    assert [entry["content"] for entry in repo.active_entries_ordered("facts")] == [
        "same",
        "other",
    ]


def test_no_files_is_a_noop(memory_dir, repo):
    assert migration.migrate_legacy_memory_files(repo, skip_in_tests=False) == 0
    assert repo.count_entries() == 0


def test_failed_import_rolls_back_and_leaves_files(memory_dir, repo, monkeypatch):
    (memory_dir / "MEMORY.md").write_text("first\n§\nsecond", encoding="utf-8")
    original = repo.add_entry
    calls = {"n": 0}

    def _fail_on_second(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("disk full")
        return original(*args, **kwargs)

    monkeypatch.setattr(repo, "add_entry", _fail_on_second)

    assert migration.migrate_legacy_memory_files(repo, skip_in_tests=False) == 0
    assert repo.count_entries() == 0
    assert (memory_dir / "MEMORY.md").exists()
    assert not (memory_dir / "MEMORY.md.bak").exists()


def test_rename_failure_keeps_data_and_skips_the_next_import(memory_dir, repo, monkeypatch):
    (memory_dir / "MEMORY.md").write_text("note", encoding="utf-8")

    def _rename_fails(*args, **kwargs):
        raise OSError("read-only directory")

    monkeypatch.setattr(migration.os, "replace", _rename_fails)

    assert migration.migrate_legacy_memory_files(repo, skip_in_tests=False) == 1
    assert repo.count_entries() == 1
    assert (memory_dir / "MEMORY.md").exists()
    assert migration.migrate_legacy_memory_files(repo, skip_in_tests=False) == 0


def test_empty_file_is_left_alone(memory_dir, repo):
    (memory_dir / "MEMORY.md").write_text("   \n", encoding="utf-8")

    assert migration.migrate_legacy_memory_files(repo, skip_in_tests=False) == 0
    assert (memory_dir / "MEMORY.md").exists()


def test_template_comment_only_file_is_not_imported(memory_dir, repo):
    (memory_dir / "FACTS.md").write_text(
        "<!-- FACTS.md: SYSTEM-LEVEL pitfalls. Keep entries short. -->\n", encoding="utf-8"
    )

    assert migration.migrate_legacy_memory_files(repo, skip_in_tests=False) == 0
    assert repo.count_entries() == 0
    assert (memory_dir / "FACTS.md").exists()


def test_splitter_strips_a_bare_header_line():
    assert migration.split_legacy_memory_content("# FACTS\n§\nfact") == ["fact"]
    assert migration.split_legacy_memory_content("first\n§\nsecond") == ["first", "second"]
    assert migration.split_legacy_memory_content("<!-- note -->") == []
