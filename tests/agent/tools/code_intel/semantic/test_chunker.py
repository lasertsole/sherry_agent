"""Unit tests for symbol-level chunking (pure, no disk or DB)."""

from __future__ import annotations

import pytest

from agent.tools.code_intel.semantic.chunker import SymbolRecord, build_chunk

pytestmark = [pytest.mark.unit, pytest.mark.timeout(60)]


def _record(**overrides) -> SymbolRecord:
    base = {
        "id": 1,
        "name": "top_func",
        "kind": "function",
        "file_path": "pkg/main.py",
        "line_start": 2,
        "line_end": 4,
        "language": "python",
        "doc_string": None,
        "source_snippet": None,
    }
    base.update(overrides)
    return SymbolRecord(**base)


def test_chunk_carries_name_kind_path_lines_and_body() -> None:
    lines = ["", "def top_func(x):", "    return x + 1", "", "after"]
    chunk = build_chunk(_record(), lines, max_chars=500)
    assert chunk is not None
    assert chunk.symbol_id == 1
    assert chunk.name == "top_func"
    assert chunk.kind == "function"
    assert chunk.file_path == "pkg/main.py"
    assert chunk.line_start == 2
    assert chunk.line_end == 4
    assert "function top_func (pkg/main.py:2)" in chunk.text
    assert "return x + 1" in chunk.text
    assert "after" not in chunk.text


def test_docstring_is_included_in_header() -> None:
    lines = ["def f():", "    pass"]
    chunk = build_chunk(
        _record(name="f", doc_string="Does a thing.", line_end=2), lines, max_chars=500
    )
    assert chunk is not None
    assert "Does a thing." in chunk.text


def test_non_embeddable_kind_is_skipped() -> None:
    assert build_chunk(_record(kind="variable"), ["x = 1"], max_chars=500) is None


def test_falls_back_to_snippet_when_lines_missing() -> None:
    chunk = build_chunk(
        _record(source_snippet="def top_func(x):\n    return x"), None, max_chars=500
    )
    assert chunk is not None
    assert "return x" in chunk.text


def test_empty_file_yields_no_chunk() -> None:
    assert build_chunk(_record(), [], max_chars=500) is None
    assert build_chunk(_record(), None, max_chars=500) is None


def test_body_is_truncated_to_max_chars() -> None:
    lines = ["def f():", *[f"    x{i} = {i}" for i in range(200)]]
    chunk = build_chunk(_record(name="f", line_end=len(lines)), lines, max_chars=80)
    assert chunk is not None
    assert len(chunk.text) <= 80


def test_zero_max_chars_disables_chunking() -> None:
    assert build_chunk(_record(), ["def f():", "    pass"], max_chars=0) is None
