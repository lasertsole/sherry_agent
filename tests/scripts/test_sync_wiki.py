"""Unit tests for the docs → GitHub Wiki generator.

The generator is a pure transformation: slugs, per-language page names, link
rewriting (relative docs links → wiki pages, source files → GitHub blob URLs),
and the four language indexes. No network and no wiki repo are needed.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from scripts import sync_wiki

pytestmark = [pytest.mark.unit]


def _write(docs: Path, rel_dir: str, title: str, body: str = "") -> None:
    folder = docs / rel_dir
    folder.mkdir(parents=True, exist_ok=True)
    for lang, filename in sync_wiki.README_FILES.items():
        (folder / filename).write_text(f"# {title} {lang}\n\n{body}", encoding="utf-8")


def test_slugify_and_page_names():
    assert sync_wiki.slugify(Path("auth")) == "auth"
    assert (
        sync_wiki.slugify(Path("long-running-tasks/gates/evidence-ledger"))
        == "long-running-tasks-gates-evidence-ledger"
    )
    assert sync_wiki.page_name("auth", "en") == "auth"
    assert sync_wiki.page_name("auth", "zh") == "auth-zh"
    assert sync_wiki.page_name("auth", "ja") == "auth-ja"
    assert sync_wiki.page_name("auth", "ko") == "auth-ko"


def test_generate_pages_links_and_navigation(tmp_path: Path):
    docs = tmp_path / "docs"
    _write(
        docs,
        "auth",
        "Auth Page",
        "Body text.\n\nSee [guide](../guide/README.md#guide-section).\n",
    )
    _write(
        docs,
        "guide",
        "Guide Page",
        "See [auth zh](../auth/README.zh.md#intro) and "
        "[source](../../agent/foo.py).\n\n"
        "```markdown\n[do not rewrite](../auth/README.md)\n```\n",
    )
    # The docs' own language switcher line must not survive next to the nav.
    (docs / "auth" / "README.md").write_text(
        "# Auth Page\n\n"
        "**English** · [中文](README.zh.md) · [한국어](README.ko.md) · [日本語](README.ja.md)\n\n"
        "Body text.\n\nSee [guide](../guide/README.md#guide-section).\n",
        encoding="utf-8",
    )
    out = tmp_path / "wiki"

    count = sync_wiki.generate(
        out, docs_root=docs, repo_root=tmp_path, repo="owner/repo", branch="dev"
    )

    assert count == 4 + 8  # four Home pages + two chapters × four languages
    home = (out / "Home.md").read_text(encoding="utf-8")
    assert "[Auth Page](auth)" in home
    assert "[中文](Home-zh)" in home

    auth = (out / "auth.md").read_text(encoding="utf-8")
    assert "**English** · [中文](auth-zh)" in auth
    assert "> [← Back to index](Home)" in auth
    assert auth.count("**English**") == 1  # the original switcher was removed

    assert "[guide](guide#guide-section)" in auth

    guide = (out / "guide.md").read_text(encoding="utf-8")
    assert "[auth zh](auth-zh#intro)" in guide
    assert "[source](https://github.com/owner/repo/blob/dev/agent/foo.py)" in guide
    assert "[do not rewrite](../auth/README.md)" in guide

    assert (out / "Home-zh.md").read_text(encoding="utf-8").startswith("# Sherry Wiki")
    assert "[中文](Home-zh)" in (out / "_Sidebar.md").read_text(encoding="utf-8")
    assert "github.com/owner/repo/tree/dev/docs" in (out / "_Footer.md").read_text(encoding="utf-8")


def test_strip_language_switcher_only_removes_the_header_line():
    markdown = (
        "# Title\n\n"
        "**English** · [中文](README.zh.md) · [한국어](README.ko.md) · [日本語](README.ja.md)\n\n"
        "Body mentions [中文](README.zh.md).\n"
    )

    stripped = sync_wiki._strip_language_switcher(markdown)

    assert stripped.startswith("# Title")
    assert "**English**" not in stripped
    assert "Body mentions [中文](README.zh.md)." in stripped
