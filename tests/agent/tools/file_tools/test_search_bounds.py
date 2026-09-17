"""Search-bounds tests: scan-level bounds on search_files (time budget, cap, prune).

Covers:
- an exhausted time budget stops the walk and the result says why it was cut
- the match cap stops collection and is reported
- pseudo-filesystem prune dirs are skipped and counted
- ordinary searches emit no scan markers (no silent truncation hint)
- the pagination contract and content-context shape are unchanged
"""

import json

import pytest

from agent.tools.file_tools.search_files import build_search_files_tool
from agent.tools.pub_base import path_utils
from config.features import TOOLS_TIMEOUTS

pytestmark = [pytest.mark.unit, pytest.mark.timeout(60)]

SESSION = "s-search-bounds"


@pytest.fixture
def virtual_root(tmp_path, monkeypatch):
    """Point ROOT_DIR at a fresh tmp directory so all searches stay in-root."""
    root = (tmp_path / "root").resolve()
    root.mkdir()
    monkeypatch.setattr(path_utils, "ROOT_DIR", root)
    return root


def _search(**kwargs) -> dict:
    return json.loads(build_search_files_tool()._core(session_id=SESSION, **kwargs))


class TestTimeBudget:
    @pytest.mark.parametrize("target", ["content", "files"])
    def test_exhausted_budget_stops_scan_and_is_reported(self, virtual_root, monkeypatch, target):
        (virtual_root / "needle.txt").write_text("needle\n", encoding="utf-8")
        monkeypatch.setitem(TOOLS_TIMEOUTS, "file_tools_search_time_budget_s", -1.0)

        result = _search(pattern="needle", target=target, path=".")

        assert result["scan_truncated"] is True
        assert result["scan_stop_reason"] == "time_budget"
        assert result["truncated"] is True
        assert "time budget" in result["hint"]
        assert result["matches" if target == "content" else "files"] == []

    def test_same_file_is_found_with_the_default_budget(self, virtual_root):
        (virtual_root / "needle.txt").write_text("needle\n", encoding="utf-8")

        result = _search(pattern="needle", target="content", path=".")

        assert [m["path"] for m in result["matches"]] == ["/needle.txt"]


class TestMatchCap:
    @pytest.mark.parametrize("target", ["content", "files"])
    def test_cap_stops_collection_and_is_reported(self, virtual_root, monkeypatch, target):
        for i in range(5):
            (virtual_root / f"needle{i}.txt").write_text("needle\n", encoding="utf-8")
        monkeypatch.setitem(TOOLS_TIMEOUTS, "file_tools_search_max_matches", 2)

        result = _search(pattern="needle", target=target, path=".")

        assert result["total_count"] == 2
        assert len(result["matches" if target == "content" else "files"]) == 2
        assert result["scan_truncated"] is True
        assert result["scan_stop_reason"] == "max_matches"
        assert result["truncated"] is True
        assert "scan limit" in result["hint"]


class TestPruneDirs:
    @pytest.mark.parametrize("target", ["content", "files"])
    def test_prune_dirs_are_skipped_and_counted(self, virtual_root, target):
        (virtual_root / "proc").mkdir()
        (virtual_root / "proc" / "needle.txt").write_text("needle\n", encoding="utf-8")
        (virtual_root / "keep").mkdir()
        (virtual_root / "keep" / "needle.txt").write_text("needle\n", encoding="utf-8")

        result = _search(pattern="needle", target=target, path=".")

        if target == "content":
            paths = [m["path"] for m in result["matches"]]
        else:
            paths = result["files"]
        assert "/keep/needle.txt" in paths
        assert all("/proc/" not in path for path in paths)
        assert result["pruned_dir_count"] == 1


class TestNormalSearchUnaffected:
    def test_small_tree_emits_no_scan_markers(self, virtual_root):
        (virtual_root / "note.txt").write_text("needle\n", encoding="utf-8")

        result = _search(pattern="needle", target="content", path=".")

        assert result["total_count"] == 1
        assert result["matches"][0]["path"] == "/note.txt"
        assert "truncated" not in result
        assert "scan_truncated" not in result
        assert "scan_stop_reason" not in result
        assert "pruned_dir_count" not in result
        assert "context_before" not in result["matches"][0]

    def test_context_lines_survive_the_scan_refactor(self, virtual_root):
        (virtual_root / "note.txt").write_text("before\nneedle\nafter\n", encoding="utf-8")

        result = _search(pattern="needle", target="content", path=".", context=1)
        match = result["matches"][0]

        assert match["context_before"] == ["before"]
        assert match["context_after"] == ["after"]
        assert "scan_truncated" not in result


class TestPaginationContract:
    def test_pages_advance_without_overlap(self, virtual_root):
        for i in range(5):
            (virtual_root / f"item{i}.txt").write_text("needle\n", encoding="utf-8")

        first = _search(pattern="needle", target="content", path=".", limit=2)
        assert len(first["matches"]) == 2
        assert first["total_count"] == 3  # page break collects offset + limit + 1
        assert first["truncated"] is True
        assert first["hint"] == "Use offset=2 to see more results."
        assert "scan_truncated" not in first

        second = _search(pattern="needle", target="content", path=".", limit=2, offset=2)
        assert [m["path"] for m in second["matches"]] == ["/item2.txt", "/item3.txt"]
        assert second["truncated"] is True
        assert second["hint"] == "Use offset=4 to see more results."

        third = _search(pattern="needle", target="content", path=".", limit=2, offset=4)
        assert [m["path"] for m in third["matches"]] == ["/item4.txt"]
        assert "truncated" not in third
