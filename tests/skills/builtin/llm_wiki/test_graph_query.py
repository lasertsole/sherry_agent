"""Graph-algorithm queries over the llm_wiki explicit graph."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = next(p for p in Path(__file__).resolve().parents if (p / "pyproject.toml").exists())
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from skills.builtin.llm_wiki.scripts import graph_query  # noqa: E402
from skills.builtin.llm_wiki.scripts.graph import build_wiki_graph  # noqa: E402

pytestmark = [pytest.mark.unit]


@pytest.fixture()
def wiki(tmp_path: Path) -> Path:
    root = tmp_path / "wiki"
    (root / "entities").mkdir(parents=True)
    (root / "concepts").mkdir(parents=True)
    (root / "entities" / "apple.md").write_text(
        "Apple makes the [[Battery|INFERRED]] and links to [Samsung](concepts/samsung).\n"
        "Uses the `Widget` component.\n",
        encoding="utf-8",
    )
    (root / "concepts" / "battery.md").write_text(
        "Battery is used by [[Apple]].\n", encoding="utf-8"
    )
    (root / "concepts" / "samsung.md").write_text(
        "Samsung makes the [[Galaxy]] phone.\n", encoding="utf-8"
    )
    (root / "entities" / "苹果.md").write_text("苹果使用[[电池]]。\n", encoding="utf-8")
    (root / "concepts" / "电池.md").write_text("电池由[[苹果]]采购。\n", encoding="utf-8")
    build_wiki_graph(wiki_root=root, force=True)
    return root


def test_path_finds_the_shortest_chain(wiki: Path):
    result = graph_query.path("Apple", "Samsung", wiki_root=wiki)
    assert result.found
    assert [hop.node.name.lower() for hop in result.hops] == ["apple", "samsung"]
    assert result.hops[1].confidence == "EXTRACTED"

    indirect = graph_query.path("Battery", "Galaxy", wiki_root=wiki)
    assert indirect.found
    assert [hop.node.name.lower() for hop in indirect.hops] == [
        "battery",
        "apple",
        "samsung",
        "galaxy",
    ]


def test_path_missing_nodes_are_a_clean_miss(wiki: Path):
    result = graph_query.path("Nothing", "Apple", wiki_root=wiki)
    assert not result.found
    assert result.hops == []


def test_god_nodes_rank_by_degree(wiki: Path):
    ranked = graph_query.god_nodes(5, wiki_root=wiki)
    assert ranked
    assert ranked[0].degree >= ranked[-1].degree
    assert ranked[0].node.name in {"Apple", "Samsung", "Battery"}


def test_explain_reports_neighbours_and_confidence(wiki: Path):
    explained = graph_query.explain("Apple", wiki_root=wiki)
    assert explained.node is not None
    names = {neighbor.node.name.lower() for neighbor in explained.neighbors}
    assert {"battery", "samsung", "widget"} <= names
    assert explained.confidence.get("INFERRED", 0) >= 1
    assert "entities/apple.md" in explained.source_files
    assert any(
        neighbor.node.name.lower() == "battery" and neighbor.confidence == "INFERRED"
        for neighbor in explained.neighbors
    )
    assert any(
        neighbor.node.name.lower() == "samsung" and neighbor.edge_type == "inline_link"
        for neighbor in explained.neighbors
    )


def test_communities_use_networkx_and_degrade(wiki: Path, monkeypatch: pytest.MonkeyPatch):
    pytest.importorskip("networkx")
    communities = graph_query.communities(wiki_root=wiki)
    assert communities
    assert communities[0].engine == "networkx"

    monkeypatch.setitem(sys.modules, "networkx", None)
    fallback = graph_query.communities(wiki_root=wiki)
    assert fallback
    assert all(community.engine == "components" for community in fallback)
    assert any("Apple" in community.symbols for community in fallback)


def test_query_recalls_english_and_chinese_nodes(wiki: Path):
    english = graph_query.query("Apple Battery", wiki_root=wiki)
    names = {node.name.lower() for node in english.nodes}
    assert {"apple", "battery"} <= names
    assert any(
        edge.src.lower() == "battery" and edge.dst.lower() == "apple" for edge in english.edges
    )

    chinese = graph_query.query("苹果 电池", wiki_root=wiki)
    chinese_names = {node.name for node in chinese.nodes}
    assert {"苹果", "电池"} <= chinese_names


def test_query_without_matches_returns_an_empty_subgraph(wiki: Path):
    result = graph_query.query("zzzz-nonexistent", wiki_root=wiki)
    assert result.nodes == []
    assert result.edges == []
