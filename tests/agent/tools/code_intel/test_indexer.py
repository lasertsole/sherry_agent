"""Unit tests for the tree-sitter indexer: symbols, edges, incremental, fail-open."""

from __future__ import annotations

from pathlib import Path

import pytest

from agent.tools.code_intel import CodeIndexer
from agent.tools.code_intel.indexer import IndexResult

pytestmark = [pytest.mark.unit, pytest.mark.timeout(60)]


def _index(indexer: CodeIndexer, root: Path) -> IndexResult:
    return indexer.index_directory(root)


def test_python_symbol_extraction(indexer: CodeIndexer, sample_repo: Path, db_rows) -> None:
    _index(indexer, sample_repo)
    rows = db_rows("SELECT name, kind, line_start FROM symbols WHERE file_path = 'pkg/main.py'")
    by_name = {row["name"]: row for row in rows}
    assert by_name["top_func"]["kind"] == "function"
    assert by_name["helper"]["kind"] == "function"
    assert by_name["MyClass"]["kind"] == "class"
    assert by_name["method_one"]["kind"] == "method"
    # method's parent resolves to the class row
    parent = db_rows(
        "SELECT parent_id FROM symbols WHERE name = 'method_one' AND file_path = 'pkg/main.py'"
    )[0]["parent_id"]
    class_id = db_rows("SELECT id FROM symbols WHERE name = 'MyClass'")[0]["id"]
    assert parent == class_id


def test_python_docstring_extracted(indexer: CodeIndexer, sample_repo: Path, db_rows) -> None:
    _index(indexer, sample_repo)
    doc = db_rows(
        "SELECT doc_string FROM symbols WHERE name = 'top_func' AND file_path = 'pkg/main.py'"
    )[0]["doc_string"]
    assert doc == "Top docstring."


def test_typescript_symbols(indexer: CodeIndexer, sample_repo: Path, db_rows) -> None:
    _index(indexer, sample_repo)
    rows = db_rows(
        "SELECT name, kind FROM symbols WHERE file_path = 'web/app.ts' ORDER BY line_start"
    )
    kinds = {row["name"]: row["kind"] for row in rows}
    assert kinds["topFunc"] == "function"
    assert kinds["Widget"] == "class"
    assert kinds["render"] == "method"
    assert kinds["layout"] == "method"
    assert kinds["arrow"] == "function"


def test_rust_and_go_symbols(indexer: CodeIndexer, sample_repo: Path, db_rows) -> None:
    _index(indexer, sample_repo)
    rust = {
        row["name"]: row["kind"]
        for row in db_rows("SELECT name, kind FROM symbols WHERE file_path = 'rs/lib.rs'")
    }
    assert rust["top_func"] == "function"
    assert rust["Thing"] == "class"
    assert rust["method_one"] == "method"
    go = {
        row["name"]: row["kind"]
        for row in db_rows("SELECT name, kind FROM symbols WHERE file_path = 'go/sample.go'")
    }
    assert go["TopFunc"] == "function"
    assert go["Widget"] == "class"
    assert go["MethodOne"] == "method"
    go_parent = db_rows(
        "SELECT parent_id FROM symbols WHERE name = 'MethodOne' AND file_path = 'go/sample.go'"
    )[0]["parent_id"]
    widget_id = db_rows(
        "SELECT id FROM symbols WHERE name = 'Widget' AND file_path = 'go/sample.go'"
    )[0]["id"]
    assert go_parent == widget_id


def test_call_edge_extraction(indexer: CodeIndexer, sample_repo: Path, db_rows) -> None:
    _index(indexer, sample_repo)
    edges = db_rows("SELECT callee_name, resolved FROM call_edges WHERE file_path = 'pkg/main.py'")
    names = {row["callee_name"] for row in edges}
    assert {"helper", "method_two", "top_func"} <= names
    resolved = {row["callee_name"] for row in edges if row["resolved"]}
    assert "helper" in resolved


def test_cross_file_call_resolution(indexer: CodeIndexer, sample_repo: Path, db_rows) -> None:
    _index(indexer, sample_repo)
    edge = db_rows(
        "SELECT callee_id, resolved FROM call_edges "
        "WHERE file_path = 'pkg/calls_top.py' AND callee_name = 'top_func'"
    )[0]
    assert edge["resolved"] == 1
    target = db_rows("SELECT name, file_path FROM symbols WHERE id = ?", (edge["callee_id"],))[0]
    assert target["name"] == "top_func"
    assert target["file_path"] == "pkg/main.py"


def test_incremental_index_mtime_skip(indexer: CodeIndexer, sample_repo: Path) -> None:
    first = _index(indexer, sample_repo)
    assert first.indexed_files > 0
    second = _index(indexer, sample_repo)
    assert second.indexed_files == 0
    assert second.skipped_files > 0
    assert second.resolved_edges == 0


def test_changed_file_is_reindexed(indexer: CodeIndexer, sample_repo: Path) -> None:
    _index(indexer, sample_repo)
    target = sample_repo / "pkg" / "main.py"
    target.write_text(
        target.read_text(encoding="utf-8") + "\ndef added_fn():\n    return 2\n", encoding="utf-8"
    )
    result = _index(indexer, sample_repo)
    assert result.indexed_files == 1


def test_deleted_file_symbols_removed(indexer: CodeIndexer, sample_repo: Path, db_rows) -> None:
    _index(indexer, sample_repo)
    (sample_repo / "pkg" / "main.py").unlink()
    result = _index(indexer, sample_repo)
    assert result.deleted_files >= 1
    assert db_rows("SELECT COUNT(*) AS n FROM symbols WHERE file_path = 'pkg/main.py'")[0]["n"] == 0


def test_prune_dirs_excluded(indexer: CodeIndexer, sample_repo: Path, db_rows) -> None:
    _index(indexer, sample_repo)
    assert (
        db_rows("SELECT COUNT(*) AS n FROM symbols WHERE file_path LIKE 'node_modules/%'")[0]["n"]
        == 0
    )


def test_unsupported_language_skipped(indexer: CodeIndexer, sample_repo: Path, db_rows) -> None:
    _index(indexer, sample_repo)
    assert (
        db_rows("SELECT COUNT(*) AS n FROM index_meta WHERE file_path = 'notes.txt'")[0]["n"] == 0
    )


def test_syntax_error_file_skipped(indexer: CodeIndexer, sample_repo: Path, db_rows) -> None:
    _index(indexer, sample_repo)
    meta = db_rows("SELECT error FROM index_meta WHERE file_path = 'pkg/broken.py'")[0]
    assert meta["error"]
    assert (
        db_rows("SELECT COUNT(*) AS n FROM symbols WHERE file_path = 'pkg/broken.py'")[0]["n"] == 0
    )


def test_oversized_file_skipped(indexer: CodeIndexer, sample_repo: Path, db_rows) -> None:
    indexer._config["code_intel_index_max_file_bytes"] = 64
    _index(indexer, sample_repo)
    meta = db_rows("SELECT error FROM index_meta WHERE file_path = 'pkg/main.py'")[0]
    assert "too large" in meta["error"]
    assert db_rows("SELECT COUNT(*) AS n FROM symbols WHERE file_path = 'pkg/main.py'")[0]["n"] == 0


def test_batch_transaction_rolls_back(
    indexer: CodeIndexer, sample_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    conn = indexer._connect()
    indexer._ensure_schema(conn)
    calls = {"count": 0}
    real_index_one = indexer._index_one

    def _flaky(connection, path, rel, mtime, size, result):
        if calls["count"] == 1:
            raise RuntimeError("boom")
        calls["count"] += 1
        real_index_one(connection, path, rel, mtime, size, result)

    monkeypatch.setattr(indexer, "_index_one", _flaky)
    pending = [
        (sample_repo / "pkg" / "main.py", "pkg/main.py", 1.0, 100),
        (sample_repo / "pkg" / "calls_top.py", "pkg/calls_top.py", 1.0, 100),
    ]
    result = IndexResult()
    indexer._flush_batch(conn, pending, result)
    assert result.error_files == 2
    assert conn.execute("SELECT COUNT(*) FROM symbols").fetchone()[0] == 0
    conn.close()


def test_index_meta_records_size_and_mtime(
    indexer: CodeIndexer, sample_repo: Path, db_rows
) -> None:
    _index(indexer, sample_repo)
    row = db_rows("SELECT mtime, size FROM index_meta WHERE file_path = 'pkg/main.py'")[0]
    assert row["mtime"] > 0
    assert row["size"] > 0
