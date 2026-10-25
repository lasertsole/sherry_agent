"""Lint keeps its original report and appends the explicit-graph metrics (L7)."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = next(p for p in Path(__file__).resolve().parents if (p / "pyproject.toml").exists())
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from skills.builtin.llm_wiki.scripts import search  # noqa: E402

pytestmark = [pytest.mark.unit]

ORIGINAL_KEYS = {
    "orphan_pages",
    "broken_links",
    "index_completeness",
    "frontmatter_issues",
    "large_pages",
    "total_pages",
    "errors",
}


@pytest.fixture()
def wiki(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "wiki"
    (root / "entities").mkdir(parents=True)
    (root / "concepts").mkdir(parents=True)
    (root / "entities" / "apple.md").write_text(
        "---\ntitle: Apple\ncreated: 2026-01-01\nupdated: 2026-01-01\ntype: entity\ntags: [x]\n"
        "---\n\nApple makes the [[Battery|INFERRED]].\n"
        "See [Samsung](../concepts/samsung.md) and the `Widget` component.\n",
        encoding="utf-8",
    )
    (root / "concepts" / "samsung.md").write_text(
        "---\ntitle: Samsung\ncreated: 2026-01-01\nupdated: 2026-01-01\ntype: concept\n"
        "tags: [x]\n---\n\nSamsung builds phones.\n",
        encoding="utf-8",
    )
    (root / "concepts" / "guess.md").write_text(
        "---\ntitle: Guess\ncreated: 2026-01-01\nupdated: 2026-01-01\ntype: concept\n"
        "tags: [x]\n---\n\nMaybe [[Battery|INFERRED]] powers it.\n",
        encoding="utf-8",
    )
    (root / "concepts" / "battery.md").write_text(
        "---\ntitle: Battery\ncreated: 2026-01-01\nupdated: 2026-01-01\ntype: concept\n"
        "tags: [x]\n---\n\nBattery is used by [[Apple]].\n",
        encoding="utf-8",
    )
    (root / "index.md").write_text("# Index\n\n- [[Apple]]\n", encoding="utf-8")
    monkeypatch.setattr(search, "get_wiki_path", lambda: root)
    return root


def test_lint_report_keeps_the_original_keys_and_appends_graph(wiki: Path):
    report = search.lint_wiki()

    assert ORIGINAL_KEYS <= set(report)
    assert report["total_pages"] == 4
    assert report["frontmatter_issues"] == []
    graph = report["graph"]
    assert graph["available"] is True
    assert graph["nodes"] >= 2
    assert graph["edges"] >= 2
    assert graph["components"] >= 1
    assert graph["largest_component"] >= 2
    assert graph["confidence"].get("EXTRACTED", 0) >= 1
    assert graph["confidence"].get("INFERRED", 0) >= 1
    assert graph["god_nodes"]
    flagged = {page["file"] for page in graph["low_confidence_pages"]}
    assert flagged == {"concepts/guess.md"}


def test_lint_graph_section_is_fail_open(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    root = tmp_path / "missing"
    monkeypatch.setattr(search, "get_wiki_path", lambda: root)
    report = search.lint_wiki()
    assert ORIGINAL_KEYS <= set(report)
    assert report["graph"]["available"] is False
