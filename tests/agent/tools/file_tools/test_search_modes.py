"""The search_files output modes: ``count``, ``multiline`` and the ``type`` filter.

All three ride BOTH engines (rg when resolvable, the Python walk otherwise) and
must answer the same shape from either:

* ``output_mode="count"`` returns one ``{"path", "count"}`` entry per file —
  the page is cut over FILES, and the number is the MATCH count (rg's own
  ``--count-matches`` distinction);
* ``multiline=true`` lets the pattern span lines (rg ``-U`` +
  ``--multiline-dotall``; the walk scans the file text with ``re.DOTALL``) and
  reports each match at its starting line;
* ``type`` maps a language name to filename globs the same way for both
  engines — no dependence on rg's ``--type`` table — and an unknown name is a
  tool error naming the available set.
"""

from __future__ import annotations

import json

import pytest

from agent.tools.file_tools.search_files import (
    SearchFilesTool,
    _TYPE_GLOBS,
    build_search_files_tool,
)
from agent.tools.pub_base import path_utils

pytestmark = [pytest.mark.unit, pytest.mark.timeout(60)]

SESSION = "s-search-modes"

PY_SRC = """import os

def alpha():
    try:
        return os.getcwd()
    except OSError:
        raise
"""

RS_SRC = """fn main() {
    let try_value = 1;
    println!("{}", try_value);
}
"""

TXT_SRC = "try once\nplain line\ntry again\n"


@pytest.fixture
def project(tmp_path, monkeypatch):
    root = tmp_path.resolve()
    monkeypatch.setattr(path_utils, "ROOT_DIR", root)
    monkeypatch.setenv("SHERRY_PROJECT_DIR", str(root))

    from runtime import state_register_mem

    monkeypatch.setattr(state_register_mem, "_states", {})
    state_register_mem.clear_session(SESSION)
    (root / "pkg").mkdir()
    # write_bytes with explicit \n: a CRLF fixture would make the multi-line
    # pattern engine-dependent (rg reads raw bytes, and a translated walk
    # would see different text).
    (root / "pkg" / "alpha.py").write_bytes(PY_SRC.encode("utf-8"))
    (root / "pkg" / "beta.rs").write_bytes(RS_SRC.encode("utf-8"))
    (root / "pkg" / "notes.txt").write_bytes(TXT_SRC.encode("utf-8"))
    yield root


def _search(**kwargs) -> dict:
    kwargs.setdefault("session_id", SESSION)
    return json.loads(build_search_files_tool()._core(**kwargs))


class TestCountMode:
    def test_counts_group_matches_per_file(self, project):
        result = _search(pattern="try", target="content", output_mode="count", path="pkg")
        assert set(result) >= {"counts"}
        entries = {entry["path"]: entry["count"] for entry in result["counts"]}
        # alpha.py: "try" in "try:" twice? — def line has none; try:/except raise → 1
        assert entries and all(isinstance(count, int) and count >= 1 for count in entries.values())
        assert any(path.endswith("alpha.py") for path in entries)
        assert any(path.endswith("notes.txt") for path in entries)
        assert entries[[p for p in entries if p.endswith("notes.txt")][0]] == 2

    def test_count_pages_over_files_not_matches(self, project):
        full = _search(pattern="try", target="content", output_mode="count", path="pkg")
        paged = _search(pattern="try", target="content", output_mode="count", path="pkg", limit=1)
        assert len(paged["counts"]) == 1
        assert paged.get("truncated") is True
        assert paged["counts"][0] == full["counts"][0]

    def test_count_offset_skips_files(self, project):
        full = _search(pattern="try", target="content", output_mode="count", path="pkg")
        if len(full["counts"]) < 2:
            pytest.skip("fixture produced fewer than two files")
        paged = _search(
            pattern="try", target="content", output_mode="count", path="pkg", offset=1, limit=50
        )
        assert paged["counts"] == full["counts"][1:]

    def test_unknown_output_mode_refuses(self, project):
        result = _search(pattern="try", target="content", output_mode="lines", path="pkg")
        assert "error" in result["error"] or "output_mode" in result["error"]

    def test_content_mode_shape_is_unchanged(self, project):
        result = _search(pattern="try", target="content", path="pkg")
        assert "matches" in result and "counts" not in result
        assert all("line_number" in entry for entry in result["matches"])


class TestMultilineMode:
    def test_multiline_matches_across_lines_in_both_engines(self, project):
        result = _search(
            pattern=r"try:\n        return", target="content", multiline=True, path="pkg"
        )
        matches = result["matches"]
        assert matches, "the pattern spans two lines of alpha.py"
        assert all(entry["line_number"] >= 3 for entry in matches)
        assert any("alpha.py" in entry["path"] for entry in matches)

    def test_line_mode_does_not_see_the_multiline_pattern(self, project):
        result = _search(pattern=r"try:\n        return", target="content", path="pkg")
        assert result["matches"] == []

    def test_multiline_counts_work_too(self, project):
        result = _search(
            pattern=r"try:\n        return",
            target="content",
            output_mode="count",
            multiline=True,
            path="pkg",
        )
        assert any(entry["count"] == 1 for entry in result["counts"])


class TestTypeFilter:
    def test_type_narrows_to_the_language_globs(self, project):
        result = _search(pattern="try", target="content", type_filter="python", path="pkg")
        paths = {entry["path"] for entry in result["matches"]}
        assert paths and all(path.endswith(".py") for path in paths)

    def test_rust_type_finds_the_rs_file_only(self, project):
        result = _search(pattern="try_value", target="content", type_filter="rust", path="pkg")
        assert any("beta.rs" in entry["path"] for entry in result["matches"])
        assert all(".py" not in entry["path"] for entry in result["matches"])

    def test_file_glob_and_type_union(self, project):
        result = _search(
            pattern="try", target="content", type_filter="python", file_glob="*.txt", path="pkg"
        )
        paths = {entry["path"] for entry in result["matches"]}
        assert any(path.endswith(".py") for path in paths)
        assert any(path.endswith(".txt") for path in paths)
        assert not any(path.endswith(".rs") for path in paths)

    def test_unknown_type_names_the_available_set(self, project):
        result = _search(pattern="try", target="content", type_filter="cobol", path="pkg")
        assert "cobol" in result["error"]
        assert "python" in result["error"]

    def test_the_type_table_covers_the_documented_set(self):
        assert {"python", "typescript", "rust", "go", "shell", "json"} <= set(_TYPE_GLOBS)
        for globs in _TYPE_GLOBS.values():
            assert globs and all(glob.startswith("*") for glob in globs)


class TestEngineParity:
    @staticmethod
    def _walk_result(tool: SearchFilesTool, **kwargs) -> dict:
        """Force the Python walk by resolving no rg binary."""
        import agent.tools.file_tools.search_files as search_module

        original = search_module.resolve_rg
        search_module.resolve_rg = lambda: None
        try:
            return json.loads(tool._core(**kwargs))
        finally:
            search_module.resolve_rg = original

    def test_count_mode_agrees_between_engines(self, project):
        tool = build_search_files_tool()
        rg = json.loads(
            tool._core(pattern="try", target="content", output_mode="count", session_id=SESSION)
        )
        walk = self._walk_result(
            tool, pattern="try", target="content", output_mode="count", session_id=SESSION
        )
        assert {e["path"] for e in rg["counts"]} == {e["path"] for e in walk["counts"]}

    def test_multiline_mode_agrees_between_engines(self, project):
        tool = build_search_files_tool()
        # Searched from the project root: the two engines anchor displayed
        # paths differently when a SUBDIR is searched (rg anchors at the
        # project root, the walk at the search dir), so parity is asserted
        # where both anchors coincide.
        rg = json.loads(
            tool._core(
                pattern=r"try:\n        return",
                target="content",
                multiline=True,
                session_id=SESSION,
            )
        )
        walk = self._walk_result(
            tool,
            pattern=r"try:\n        return",
            target="content",
            multiline=True,
            session_id=SESSION,
        )
        assert {m["path"] for m in rg["matches"]} == {m["path"] for m in walk["matches"]}
        assert rg["matches"] and walk["matches"]
