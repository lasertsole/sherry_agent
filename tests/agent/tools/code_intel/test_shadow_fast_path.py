"""Shadow-git fast path, lazy counts, and the CodeQuery result cache.

Every test that exercises the fast path first creates a real shadow-git store
with ``take_snapshot``; without one the indexer must behave exactly like the
original full walk (the existing ``test_indexer.py`` suite covers that side).
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from agent.tools.code_intel import CodeIndexer, CodeQuery
from agent.tools.pub_base.shadow_git import (
    diff_between_commits,
    diff_between_commits_with_status,
    take_snapshot,
)
from pub.git_head import shadow_head

pytestmark = [pytest.mark.unit, pytest.mark.timeout(120)]


def _snapshot(root: Path, reason: str = "test") -> str:
    sha = take_snapshot(root, reason)
    assert sha, "take_snapshot failed"
    return sha


def _append(path: Path, text: str) -> None:
    path.write_text(path.read_text(encoding="utf-8") + text, encoding="utf-8")


def _summary_head(db_path: Path) -> str | None:
    conn = sqlite3.connect(str(db_path))
    try:
        row = conn.execute("SELECT shadow_head FROM index_meta_summary WHERE id = 1").fetchone()
        return row[0] if row else None
    finally:
        conn.close()


def _count(db_path: Path, sql: str, params: tuple = ()) -> int:
    conn = sqlite3.connect(str(db_path))
    try:
        return int(conn.execute(sql, params).fetchone()[0])
    finally:
        conn.close()


class TestShadowFastPath:
    def test_first_index_records_watermark_then_short_circuits(
        self, indexer: CodeIndexer, sample_repo: Path, index_db: Path
    ) -> None:
        _snapshot(sample_repo)
        first = indexer.index_directory(sample_repo)
        assert first.indexed_files > 0
        assert _summary_head(index_db) == shadow_head(sample_repo)

        second = indexer.index_directory(sample_repo)

        assert second.changed is False
        assert second.indexed_files == 0
        assert second.skipped_files == 0, "a short-circuit must not walk the tree"

    def test_only_the_changed_file_is_reindexed(
        self, indexer: CodeIndexer, sample_repo: Path, index_db: Path
    ) -> None:
        _snapshot(sample_repo)
        indexer.index_directory(sample_repo)
        _append(sample_repo / "pkg/main.py", "\ndef added_by_shadow_fast_path():\n    return 7\n")
        _snapshot(sample_repo, "write")

        result = indexer.index_directory(sample_repo)

        assert result.indexed_files == 1
        assert (
            _count(
                index_db,
                "SELECT COUNT(*) FROM symbols WHERE name = 'added_by_shadow_fast_path'",
            )
            == 1
        )
        assert _summary_head(index_db) == shadow_head(sample_repo)

    def test_a_deleted_file_is_dropped(
        self, indexer: CodeIndexer, sample_repo: Path, index_db: Path
    ) -> None:
        _snapshot(sample_repo)
        indexer.index_directory(sample_repo)
        (sample_repo / "pkg/calls_top.py").unlink()
        _snapshot(sample_repo, "delete")

        result = indexer.index_directory(sample_repo)

        assert result.deleted_files >= 1
        assert (
            _count(index_db, "SELECT COUNT(*) FROM symbols WHERE file_path = 'pkg/calls_top.py'")
            == 0
        )

    def test_a_rename_moves_the_symbols(
        self, indexer: CodeIndexer, sample_repo: Path, index_db: Path
    ) -> None:
        _snapshot(sample_repo)
        indexer.index_directory(sample_repo)
        (sample_repo / "pkg/calls_top.py").rename(sample_repo / "pkg/calls_moved.py")
        _snapshot(sample_repo, "rename")

        result = indexer.index_directory(sample_repo)

        assert result.changed is True
        assert (
            _count(index_db, "SELECT COUNT(*) FROM symbols WHERE file_path = 'pkg/calls_top.py'")
            == 0
        )
        assert (
            _count(index_db, "SELECT COUNT(*) FROM symbols WHERE file_path = 'pkg/calls_moved.py'")
            >= 1
        )

    def test_an_external_edit_falls_back_to_the_walk(
        self, indexer: CodeIndexer, sample_repo: Path, index_db: Path
    ) -> None:
        _snapshot(sample_repo)
        indexer.index_directory(sample_repo)
        # No snapshot: HEAD is unchanged, exactly like an IDE edit.
        _append(sample_repo / "pkg/main.py", "\ndef external_edit_probe():\n    return 1\n")

        result = indexer.index_directory(sample_repo)

        assert result.indexed_files == 1
        assert (
            _count(index_db, "SELECT COUNT(*) FROM symbols WHERE name = 'external_edit_probe'") == 1
        )

    def test_force_bypasses_the_fast_path(self, indexer: CodeIndexer, sample_repo: Path) -> None:
        _snapshot(sample_repo)
        indexer.index_directory(sample_repo)

        forced = indexer.index_directory(sample_repo, force=True)

        assert forced.indexed_files > 0

    def test_no_shadow_store_keeps_the_walk_and_writes_no_watermark(
        self, indexer: CodeIndexer, sample_repo: Path, index_db: Path
    ) -> None:
        first = indexer.index_directory(sample_repo)
        second = indexer.index_directory(sample_repo)

        assert first.indexed_files > 0
        assert second.skipped_files > 0
        assert _summary_head(index_db) is None

    def test_the_read_only_diff_never_creates_a_store(self, tmp_path: Path) -> None:
        repo = tmp_path / "plain"
        repo.mkdir()
        (repo / "a.py").write_text("x = 1\n", encoding="utf-8")

        assert diff_between_commits(repo, "a", "b") == []
        assert diff_between_commits_with_status(repo, "a", "b") == []
        assert not (repo / ".sherry").exists()


class TestLazyCounts:
    def test_counts_are_fetched_only_when_read(
        self, indexer: CodeIndexer, sample_repo: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls = {"n": 0}
        original = indexer._counts_loader

        def counting() -> dict[str, int]:
            calls["n"] += 1
            return original()

        monkeypatch.setattr(indexer, "_counts_loader", counting)
        result = indexer.index_directory(sample_repo)

        assert calls["n"] == 0
        assert result.total_symbols > 0
        assert calls["n"] == 1
        assert result.total_calls > 0  # cached
        assert calls["n"] == 1

    def test_schema_is_ensured_once(self, indexer: CodeIndexer, index_db: Path) -> None:
        conn = indexer._connect()
        try:
            indexer._ensure_schema(conn)
            assert indexer._schema_ensured is True
            indexer._ensure_schema(conn)  # no-op
        finally:
            conn.close()


class TestQueryCache:
    def _count_connects(self, query: CodeQuery, monkeypatch: pytest.MonkeyPatch) -> dict:
        calls = {"n": 0}
        original = query._connect

        def counting():
            calls["n"] += 1
            return original()

        monkeypatch.setattr(query, "_connect", counting)
        return calls

    def test_a_repeated_query_is_served_from_cache(
        self, sample_repo: Path, index_db: Path, intel_config, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _snapshot(sample_repo)
        query = CodeQuery(str(index_db), intel_config)
        calls = self._count_connects(query, monkeypatch)

        first = query.callers("helper", sample_repo)
        second = query.callers("helper", sample_repo)

        assert first == second
        assert calls["n"] == 1, "the second identical query must not open the DB"

    def test_a_snapshot_change_invalidates_the_cache(
        self, sample_repo: Path, index_db: Path, intel_config, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _snapshot(sample_repo)
        query = CodeQuery(str(index_db), intel_config)
        calls = self._count_connects(query, monkeypatch)
        query.callers("helper", sample_repo)
        assert calls["n"] == 1

        _append(sample_repo / "pkg/main.py", "\ndef cache_probe():\n    return 2\n")
        _snapshot(sample_repo, "write")

        query.callers("added_by_cache_probe", sample_repo)
        assert calls["n"] == 2

    def test_an_external_edit_invalidates_the_cache_through_the_generation(
        self, sample_repo: Path, index_db: Path, intel_config, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _snapshot(sample_repo)
        query = CodeQuery(str(index_db), intel_config)
        calls = self._count_connects(query, monkeypatch)
        query.callers("helper", sample_repo)
        assert calls["n"] == 1

        # No snapshot: the shadow HEAD does not move, only the index generation.
        _append(sample_repo / "pkg/main.py", "\ndef external_cache_probe():\n    return 3\n")

        query.callers("external_cache_probe", sample_repo)
        assert calls["n"] == 2

    def test_no_shadow_store_disables_the_cache(
        self, sample_repo: Path, index_db: Path, intel_config, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        query = CodeQuery(str(index_db), intel_config)
        calls = self._count_connects(query, monkeypatch)

        query.callers("helper", sample_repo)
        query.callers("helper", sample_repo)

        assert calls["n"] == 2, "without a shadow HEAD every query re-reads the DB"


class TestIncrementalResolve:
    def test_the_fast_path_passes_the_changed_files_to_the_resolver(
        self,
        indexer: CodeIndexer,
        sample_repo: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _snapshot(sample_repo)
        indexer.index_directory(sample_repo)
        _append(sample_repo / "pkg/main.py", "\ndef resolve_probe():\n    return helper(1)\n")
        _snapshot(sample_repo, "write")

        seen: dict = {}
        original = indexer._resolve_call_edges

        def recording(conn, *, file_paths=None, names=None):
            seen["file_paths"] = file_paths
            seen["names"] = names
            return original(conn, file_paths=file_paths, names=names)

        monkeypatch.setattr(indexer, "_resolve_call_edges", recording)
        indexer.index_directory(sample_repo)

        assert seen["file_paths"] == ["pkg/main.py"]
        assert "resolve_probe" in (seen["names"] or set())
