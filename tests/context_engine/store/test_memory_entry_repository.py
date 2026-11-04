"""SQLite repository contract for long-term memory entries."""

from __future__ import annotations

import time

import pytest

from context_engine.store.memory_entry_repository import (
    ARCHIVE_REASONS,
    MEMORY_TARGETS,
    MemoryEntryRepository,
)

pytestmark = [pytest.mark.unit, pytest.mark.timeout(60)]


@pytest.fixture
def repo(tmp_path):
    repository = MemoryEntryRepository(db_path=tmp_path / "memory.db")
    yield repository
    repository.close()


def test_add_and_get_entry(repo):
    entry = repo.add_entry(
        "memory",
        "Environment: Python 3.13",
        entry_type="reference",
        origin_session="sess-a",
    )

    assert entry["id"] > 0
    assert entry["target"] == "memory"
    assert entry["content"] == "Environment: Python 3.13"
    assert entry["entry_type"] == "reference"
    assert entry["origin_session"] == "sess-a"
    assert entry["char_count"] == len("Environment: Python 3.13")
    assert entry["usage_count"] == 0
    assert entry["archived"] == 0
    assert repo.get_entry(entry["id"]) == entry


def test_list_entries_filters_target_and_archive(repo):
    memory = repo.add_entry("memory", "note")
    user = repo.add_entry("user", "User: likes tea")
    archived = repo.add_entry("memory", "old note")
    repo.archive_entry(archived["id"], "manual")

    active = repo.list_entries(limit=None)
    assert {entry["id"] for entry in active} == {memory["id"], user["id"]}
    assert {entry["id"] for entry in repo.list_entries("memory", limit=None)} == {memory["id"]}

    everything = repo.list_entries(include_archived=True, limit=None)
    assert {entry["id"] for entry in everything} == {memory["id"], user["id"], archived["id"]}
    assert repo.count() == 2
    assert repo.count(include_archived=True) == 3


def test_created_order_is_deterministic_for_the_prompt(repo):
    first = repo.add_entry("memory", "first", created_at=1_000)
    second = repo.add_entry("memory", "second", created_at=1_000)

    ordered = repo.active_entries_ordered("memory")

    assert [entry["id"] for entry in ordered] == [first["id"], second["id"]]
    assert [entry["content"] for entry in ordered] == ["first", "second"]


def test_sort_modes(repo):
    old = repo.add_entry("memory", "old", created_at=1_000)
    new = repo.add_entry("memory", "new", created_at=2_000)
    repo.increment_usage(old["id"])
    repo.increment_usage(old["id"])
    repo.increment_usage(new["id"])

    by_created = repo.list_entries(sort="created", order="desc", limit=None)
    assert [entry["id"] for entry in by_created] == [new["id"], old["id"]]

    by_usage = repo.list_entries(sort="usage", order="desc", limit=None)
    assert by_usage[0]["id"] == old["id"]

    by_last_used = repo.list_entries(sort="last_used", order="desc", limit=None)
    assert {entry["id"] for entry in by_last_used} == {old["id"], new["id"]}


def test_list_rejects_an_unknown_sort(repo):
    with pytest.raises(ValueError):
        repo.list_entries(sort="nope")


def test_update_entry_clears_embedding_and_updates_char_count(repo):
    entry = repo.add_entry("memory", "first")
    repo.db.execute(
        "UPDATE memory_entries SET embedding = ?, embedding_model = ?, embedding_dim = ? "
        "WHERE id = ?",
        (b"vec", "bge-m3", 1024, entry["id"]),
    )

    updated = repo.update_entry(entry["id"], content="second", entry_type="project")

    assert updated is not None
    assert updated["content"] == "second"
    assert updated["char_count"] == len("second")
    assert updated["entry_type"] == "project"
    assert updated["embedding"] is None
    assert updated["embedding_model"] is None
    assert updated["embedding_dim"] is None
    assert updated["updated_at"] is not None


def test_update_missing_entry_returns_none(repo):
    assert repo.update_entry(999_999, content="x") is None


def test_archive_and_restore(repo):
    entry = repo.add_entry("memory", "note")

    archived = repo.archive_entry(entry["id"], "evicted")
    assert archived is not None
    assert archived["archived"] == 1
    assert archived["archived_reason"] == "evicted"
    assert archived["archived_at"] is not None
    assert repo.list_entries("memory", limit=None) == []

    restored = repo.restore_entry(entry["id"])
    assert restored is not None
    assert restored["archived"] == 0
    assert restored["archived_reason"] is None
    assert restored["archived_at"] is None
    assert repo.list_entries("memory", limit=None)[0]["id"] == entry["id"]


def test_archive_rejects_an_unknown_reason(repo):
    entry = repo.add_entry("memory", "note")
    with pytest.raises(ValueError):
        repo.archive_entry(entry["id"], "whatever")
    assert set(ARCHIVE_REASONS)


def test_increment_usage_updates_count_and_last_used(repo):
    entry = repo.add_entry("memory", "note")

    repo.increment_usage(entry["id"])
    repo.increment_usage(entry["id"])

    refreshed = repo.get_entry(entry["id"])
    assert refreshed is not None
    assert refreshed["usage_count"] == 2
    assert refreshed["last_used"] is not None


def test_increment_usage_ignores_archived_rows(repo):
    entry = repo.add_entry("memory", "note")
    repo.archive_entry(entry["id"], "manual")

    repo.increment_usage(entry["id"])

    refreshed = repo.get_entry(entry["id"])
    assert refreshed is not None
    assert refreshed["usage_count"] == 0


def test_get_entries_by_ids_preserves_creation_order(repo):
    first = repo.add_entry("memory", "first", created_at=1_000)
    second = repo.add_entry("user", "second", created_at=2_000)

    rows = repo.get_entries_by_ids([second["id"], first["id"]])

    assert [row["id"] for row in rows] == [first["id"], second["id"]]
    assert repo.get_entries_by_ids([]) == []


def test_find_active_is_case_sensitive_substring(repo):
    repo.add_entry("memory", "Use uv run for scripts")
    repo.add_entry("memory", "Uv cache lives in the home dir")

    assert [e["content"] for e in repo.find_active("memory", "uv")] == ["Use uv run for scripts"]
    assert repo.find_active("memory", "missing") == []


def test_total_char_count_only_counts_active_rows(repo):
    entry = repo.add_entry("memory", "abc")
    repo.add_entry("memory", "defg")
    repo.archive_entry(entry["id"], "manual")

    assert repo.total_char_count("memory") == 4


def test_stats_reports_active_and_archived(repo):
    first = repo.add_entry("memory", "abc")
    repo.add_entry("memory", "defg")
    repo.increment_usage(first["id"])
    archived = repo.add_entry("user", "old")
    repo.archive_entry(archived["id"], "manual")

    stats = repo.stats()

    assert set(stats) == set(MEMORY_TARGETS)
    assert stats["memory"] == {"count": 2, "chars": 7, "avg_usage": 0.5, "archived": 0}
    assert stats["user"] == {"count": 0, "chars": 0, "avg_usage": 0.0, "archived": 1}
    assert stats["facts"] == {"count": 0, "chars": 0, "avg_usage": 0.0, "archived": 0}


def test_prune_unused_archives_only_expired_rows(repo):
    now_ms = int(time.time() * 1000)
    old = repo.add_entry("memory", "old", created_at=now_ms - 200 * 86_400_000)
    used_recently = repo.add_entry("memory", "used recently", created_at=now_ms - 200 * 86_400_000)
    repo.increment_usage(used_recently["id"], used_at=now_ms)
    fresh = repo.add_entry("memory", "fresh", created_at=now_ms)

    archived = repo.prune_unused(90)

    assert archived == 1
    assert repo.get_entry(old["id"])["archived"] == 1
    assert repo.get_entry(old["id"])["archived_reason"] == "evicted"
    assert repo.get_entry(used_recently["id"])["archived"] == 0
    assert repo.get_entry(fresh["id"])["archived"] == 0


def test_prune_unused_is_bounded_by_batch_size(repo):
    now_ms = int(time.time() * 1000)
    for index in range(3):
        repo.add_entry("memory", f"old-{index}", created_at=now_ms - 200 * 86_400_000)

    assert repo.prune_unused(90, batch_size=2) == 2
    assert repo.prune_unused(90, batch_size=2) == 1


def test_settings_roundtrip(repo):
    assert repo.get_settings() == {}

    repo.set_settings({"memory_char_limit": 1234, "max_unused_days": 7})

    assert repo.get_settings() == {"memory_char_limit": "1234", "max_unused_days": "7"}
    repo.set_settings({"memory_char_limit": 99})
    assert repo.get_settings()["memory_char_limit"] == "99"


def test_private_connection_is_isolated(tmp_path):
    first = MemoryEntryRepository(db_path=tmp_path / "first.db")
    second = MemoryEntryRepository(db_path=tmp_path / "second.db")
    try:
        first.add_entry("memory", "only in first")
        assert first.count_entries() == 1
        assert second.count_entries() == 0
    finally:
        first.close()
        second.close()
