"""Unit tests for the code_intel query engine (explore/callers/callees/impact)."""

from __future__ import annotations

from pathlib import Path

import pytest

from agent.tools.code_intel import CodeQuery

pytestmark = [pytest.mark.unit, pytest.mark.timeout(60)]


def test_explore_exact_match_ranks_first(query: CodeQuery, sample_repo: Path) -> None:
    result = query.explore("top_func", sample_repo)
    assert result.found
    top = result.entries[0].symbol
    assert top.name == "top_func"
    assert top.score == 1.0
    assert "def top_func" in result.entries[0].source


def test_explore_fuzzy_match(query: CodeQuery, sample_repo: Path) -> None:
    result = query.explore("top_fun", sample_repo)
    assert result.found
    assert any(entry.symbol.name in {"top_func", "topFunc", "TopFunc"} for entry in result.entries)


def test_explore_empty_result_suggests_search_files(query: CodeQuery, sample_repo: Path) -> None:
    result = query.explore("zzz_missing_symbol_zzz", sample_repo)
    assert not result.found
    assert result.suggestion is not None
    assert "search_files" in result.suggestion


def test_explore_includes_callers_and_callees(query: CodeQuery, sample_repo: Path) -> None:
    entry = next(
        e for e in query.explore("helper", sample_repo).entries if e.symbol.name == "helper"
    )
    assert any("top_func" in caller for caller in entry.callers)


def test_callers_returns_all_callers(query: CodeQuery, sample_repo: Path) -> None:
    callers = query.callers("helper", sample_repo)
    names = {caller.name for caller in callers}
    assert {"top_func", "TopFunc"} <= names
    assert all(caller.call_line > 0 for caller in callers)


def test_callees_returns_resolved_calls(query: CodeQuery, sample_repo: Path) -> None:
    callees = query.callees("top_func", sample_repo)
    helper = next(callee for callee in callees if callee.name == "helper")
    assert helper.resolved is True
    assert helper.target_file == "pkg/main.py"


def test_callers_empty_for_unknown_symbol(query: CodeQuery, sample_repo: Path) -> None:
    assert query.callers("does_not_exist_at_all", sample_repo) == []


def test_impact_depth_limit(query: CodeQuery, sample_repo: Path) -> None:
    query._config["code_intel_call_graph_max_depth"] = 2
    result = query.impact("base_fn", sample_repo)
    depths = {entry.depth for entry in result.entries}
    assert depths == {1, 2}
    names = {entry.symbol.name for entry in result.entries}
    assert "level1_fn" in names
    assert "level2_fn" in names
    assert "level3_fn" not in names
    assert result.truncated is True


def test_impact_full_depth(query: CodeQuery, sample_repo: Path) -> None:
    result = query.impact("base_fn", sample_repo)
    names = {entry.symbol.name for entry in result.entries}
    assert {"level1_fn", "level2_fn", "level3_fn"} <= names
    assert all(entry.depth <= result.max_depth for entry in result.entries)


def test_impact_unknown_symbol_is_empty(query: CodeQuery, sample_repo: Path) -> None:
    result = query.impact("no_such_symbol_xyz", sample_repo)
    assert result.entries == []
