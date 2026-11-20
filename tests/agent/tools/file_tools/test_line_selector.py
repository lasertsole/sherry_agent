"""The GJC line selector: ``path:50-200`` on a read_file argument.

Pinned on both layers:

* the parser (``file_tools/line_selector.py``) — ranges, multi-ranges, single
  lines, ``:conflicts``, ``:raw``; the Windows drive-letter colon stays a path
  (``C:\\file.py``) while a real selector after it (``C:\\file.py:50-200``)
  splits; malformed specs refuse instead of reading something wrong; conflict
  hunks run from ``<<<<<<<`` to their ``>>>>>>>`` (or EOF for a half-resolved
  file);
* the tool integration — a selector overrides offset/limit, a single range
  keeps every paging behavior (token budget, whole-read refusal bypass),
  multi-range/conflict views are discontiguous (joined with ``...``), never
  license a write and clamp past-EOF ranges, ``:raw`` drops the line-number
  prefix, and ``archive.zip:10-20`` is still refused at the schema level.
"""

from __future__ import annotations

import json

import pytest

from agent.tools.file_tools.line_selector import (
    conflict_blocks,
    parse_line_selector,
)
from agent.tools.file_tools.read_file import build_read_file_tool
from agent.tools.file_tools.read_file import ReadFileInput
from agent.tools.pub_base import path_utils
from agent.tools.pub_base import read_state

pytestmark = [pytest.mark.unit, pytest.mark.timeout(60)]

SESSION = "s-line-selector"


# ─────────────────────────────────────────────────────────────────────────────
# Parser
# ─────────────────────────────────────────────────────────────────────────────
class TestParser:
    def test_a_single_range_splits(self):
        path, selector, error = parse_line_selector("src/app.py:50-200")
        assert path == "src/app.py"
        assert error is None
        assert selector is not None and selector.ranges == ((50, 200),)
        assert selector.kind is None

    def test_a_single_line_is_a_one_line_range(self):
        _path, selector, _error = parse_line_selector("src/app.py:42")
        assert selector is not None and selector.ranges == ((42, 42),)

    def test_multi_ranges_preserve_order(self):
        _path, selector, _error = parse_line_selector("src/app.py:50-200,960-973")
        assert selector is not None
        assert selector.ranges == ((50, 200), (960, 973))
        assert selector.is_selective

    def test_conflicts_and_raw_kinds(self):
        _path, conflicts, _e = parse_line_selector("src/app.py:conflicts")
        _path2, raw, _e2 = parse_line_selector("src/app.py:raw")
        assert conflicts is not None and conflicts.kind == "conflicts"
        assert raw is not None and raw.kind == "raw"
        assert raw.wants_line_numbers is False
        assert conflicts.is_selective

    def test_a_windows_drive_letter_stays_a_path(self):
        for weird in ("C:\\file.py", "c:\\temp\\x", "C:/file.py"):
            path, selector, error = parse_line_selector(weird)
            assert path == weird and selector is None and error is None

    def test_a_selector_after_a_drive_letter_splits(self):
        path, selector, error = parse_line_selector("C:\\file.py:50-200")
        assert path == "C:\\file.py"
        assert selector is not None and selector.ranges == ((50, 200),)
        assert error is None

    def test_a_non_matching_suffix_stays_a_path(self):
        for weird in ("src/app.py", "weird:name.txt", "src/app.py:evil"):
            path, selector, error = parse_line_selector(weird)
            assert path == weird and selector is None and error is None

    @pytest.mark.parametrize("bad", ["src/app.py:200-50", "src/app.py:0-10"])
    def test_malformed_ranges_refuse(self, bad):
        _path, _selector, error = parse_line_selector(bad)
        assert error is not None and "ascending" in error

    def test_a_negative_suffix_is_not_a_selector_at_all(self):
        # "-5" fails the range grammar, so the whole string stays a path —
        # the ordinary machinery answers "file not found", not a parse error.
        path, selector, error = parse_line_selector("src/app.py:-5")
        assert path == "src/app.py:-5" and selector is None and error is None

    def test_conflict_blocks_scan(self):
        lines = [
            "before\n",
            "<<<<<<< HEAD\n",
            "ours\n",
            "=======\n",
            "theirs\n",
            ">>>>>>> branch\n",
            "after\n",
            "<<<<<<< HEAD\n",
            "unresolved\n",
        ]
        assert conflict_blocks(lines) == ((2, 6), (8, 9))


# ─────────────────────────────────────────────────────────────────────────────
# Tool integration
# ─────────────────────────────────────────────────────────────────────────────
CONTENT = "\n".join(f"line {i} here" for i in range(1, 41)) + "\n"


@pytest.fixture
def project(tmp_path, monkeypatch):
    root = tmp_path.resolve()
    monkeypatch.setattr(path_utils, "ROOT_DIR", root)
    monkeypatch.setenv("SHERRY_PROJECT_DIR", str(root))

    from runtime import state_register_mem

    monkeypatch.setattr(state_register_mem, "_states", {})
    state_register_mem.clear_session(SESSION)
    read_state.forget_all()
    target = root / "app.txt"
    target.write_text(CONTENT, encoding="utf-8")
    yield root
    read_state.forget_all()


def _read(path: str, **kwargs) -> dict:
    return json.loads(build_read_file_tool()._core(path, session_id=SESSION, **kwargs))


class TestSelectorIntegration:
    def test_a_single_range_matches_offset_limit(self, project):
        selected = _read("app.txt:10-15")
        paged = _read("app.txt", offset=10, limit=6)
        assert selected["content"] == paged["content"]
        assert selected["total_lines"] == 40
        # A range in the middle is truncated like the equivalent page — the
        # file continues past line 15.
        assert selected["truncated"] == paged["truncated"]
        assert selected["hint"] == paged["hint"]

    def test_a_selector_overrides_offset_and_limit(self, project):
        result = _read("app.txt:5-8", offset=30, limit=10)
        assert "line 5 here" in result["content"]
        assert "line 30 here" not in result["content"]

    def test_multi_ranges_join_discontiguous_windows(self, project):
        result = _read("app.txt:2-4,38-40")
        assert result.get("selected") is True
        assert "line 2 here" in result["content"]
        assert "line 40 here" in result["content"]
        assert "line 10 here" not in result["content"]
        assert "...\n" in result["content"], "the windows are separated"
        assert result["truncated"] is False

    def test_ranges_past_eof_are_clamped_with_a_hint(self, project):
        # A SELECTIVE read clamps and reports; a single range just stops at
        # the end of the file (the ordinary pagination machinery).
        result = _read("app.txt:38-40,50-60")
        assert "line 38 here" in result["content"] and "line 40 here" in result["content"]
        assert result["truncated"] is True
        assert "clamped" in result["hint"]

    def test_a_single_range_past_eof_just_ends(self, project):
        result = _read("app.txt:39-99")
        assert "line 39 here" in result["content"] and "line 40 here" in result["content"]
        assert result["truncated"] is False

    def test_conflicts_returns_only_hunks(self, project):
        conflicted = project / "conflicted.txt"
        conflicted.write_text(
            "keep\n<<<<<<< HEAD\nours\n=======\ntheirs\n>>>>>>> feature\nmore\n",
            encoding="utf-8",
        )
        result = _read("conflicted.txt:conflicts")
        assert result["content"]
        for marker_line in ("ours", "theirs"):
            assert marker_line in result["content"]
        assert "keep" not in result["content"] and "more" not in result["content"]

    def test_conflicts_with_no_markers_answers_empty(self, project):
        result = _read("app.txt:conflicts")
        assert result["content"] == ""
        assert "no conflict markers" in result["note"]

    def test_raw_drops_line_numbers(self, project):
        raw = _read("app.txt:raw")
        assert "line 1 here" in raw["content"]
        assert "|line 1" not in raw["content"] and "1|line" not in raw["content"]
        # The raw read completed (and licensed the file), so drop the license
        # before the numbered comparison — an unchanged re-read would stub.
        read_state.forget_all()
        numbered = _read("app.txt", limit=5)
        assert "1|line 1 here" in numbered["content"]

    def test_a_selective_read_licenses_nothing(self, project):
        _read("app.txt:2-4,38-40")
        from agent.tools.pub_base.read_state import licensed

        assert licensed(SESSION, project / "app.txt") is None

    def test_a_whole_raw_read_licenses_the_write(self, project):
        _read("app.txt:raw")
        from agent.tools.pub_base.read_state import licensed

        assert licensed(SESSION, project / "app.txt") is not None

    def test_binary_refusal_survives_a_selector(self):
        with pytest.raises(ValueError, match="binary"):
            ReadFileInput(file_path="archive.zip:10-20")

    def test_malformed_selector_refuses_as_a_tool_error(self, project):
        result = _read("app.txt:200-50")
        assert "ascending" in result["error"]

    def test_a_missing_path_with_a_selector_reports_the_path(self, project):
        result = _read("ghost.txt:1-5")
        assert "File not found" in result["error"]
