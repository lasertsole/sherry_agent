"""Explicit graph construction from markdown (wikilinks/links/mentions/frontmatter)."""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

REPO_ROOT = next(p for p in Path(__file__).resolve().parents if (p / "pyproject.toml").exists())
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from skills.builtin.llm_wiki.scripts.graph import (  # noqa: E402
    build_wiki_graph,
    get_graph_db_path,
)

pytestmark = [pytest.mark.unit]


@pytest.fixture()
def wiki(tmp_path: Path) -> Path:
    root = tmp_path / "wiki"
    (root / "entities").mkdir(parents=True)
    (root / "concepts").mkdir(parents=True)
    (root / "raw" / "papers").mkdir(parents=True)
    (root / "entities" / "apple.md").write_text(
        "---\ntitle: Apple\n---\n\n"
        "Apple makes the [[Battery|INFERRED]] and links to [Samsung](concepts/samsung).\n"
        "The `Widget` component uses `pkg.Widget`.\n",
        encoding="utf-8",
    )
    (root / "concepts" / "battery.md").write_text(
        "---\ntitle: Battery\n---\n\nBattery is used by [[Apple]].\n",
        encoding="utf-8",
    )
    (root / "concepts" / "samsung.md").write_text(
        "---\n"
        "title: Samsung\n"
        "extracted_entities: [Galaxy]\n"
        "extracted_relations:\n"
        "  - {src: Samsung, dst: Galaxy, type: makes, confidence: EXTRACTED, "
        "evidence: 'Samsung makes the Galaxy'}\n"
        "---\n\nSamsung makes the Galaxy.\n",
        encoding="utf-8",
    )
    (root / "raw" / "papers" / "note.md").write_text(
        "---\ntitle: Note\n---\n\nNo links here.\n", encoding="utf-8"
    )
    (root / "index.md").write_text("# Index\n\n- [[Apple]]\n", encoding="utf-8")
    return root


def _edges(root: Path) -> list[tuple[str, str, str, str]]:
    conn = sqlite3.connect(str(get_graph_db_path(root)))
    conn.row_factory = sqlite3.Row
    try:
        return [
            (row["src"], row["dst"], row["edge_type"], row["confidence"])
            for row in conn.execute(
                "SELECT s.name AS src, d.name AS dst, e.edge_type, e.confidence "
                "FROM wiki_edges e JOIN wiki_nodes s ON s.id = e.src_id "
                "JOIN wiki_nodes d ON d.id = e.dst_id"
            )
        ]
    finally:
        conn.close()


def test_graph_extracts_all_markdown_edge_kinds(wiki: Path):
    result = build_wiki_graph(wiki_root=wiki, force=True)
    assert result["errors"] == []
    assert result["indexed_files"] >= 4
    edges = _edges(wiki)
    lookup = {(src.lower(), dst.lower(), kind): confidence for src, dst, kind, confidence in edges}

    assert lookup[("apple", "battery", "wikilink")] == "INFERRED"
    assert lookup[("battery", "apple", "wikilink")] == "EXTRACTED"
    assert lookup[("apple", "samsung", "inline_link")] == "EXTRACTED"
    assert lookup[("apple", "widget", "mention")] == "EXTRACTED"
    assert lookup[("samsung", "galaxy", "makes")] == "EXTRACTED"


def test_graph_skips_unchanged_files_and_handles_deletion(wiki: Path):
    first = build_wiki_graph(wiki_root=wiki, force=True)
    second = build_wiki_graph(wiki_root=wiki)
    assert second["indexed_files"] == 0
    assert second["skipped_files"] >= 4
    assert second["nodes"] == first["nodes"]

    (wiki / "concepts" / "samsung.md").unlink()
    third = build_wiki_graph(wiki_root=wiki)
    assert third["removed_files"] == 1
    names = {
        row[0]
        for row in sqlite3.connect(str(get_graph_db_path(wiki))).execute(
            "SELECT name FROM wiki_nodes"
        )
    }
    assert "samsung" not in names


def test_graph_is_a_no_op_without_a_wiki(tmp_path: Path):
    result = build_wiki_graph(wiki_root=tmp_path / "absent")
    assert result["indexed_files"] == 0
    assert result["errors"]
