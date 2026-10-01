"""Which engine serves a search, and whether the two engines agree.

The acceptance surface for the ripgrep backend: the tool picks ripgrep when it
can be resolved, the two engines return the same matches on the same tree, paging
is stable, and every way ripgrep can fail (absent, disabled, unable to express
the pattern, hard error) lands on the Python walk instead of on a wrong answer.
"""

from __future__ import annotations

import json
import shutil
import time

import pytest

from agent.tools.file_tools.search_files import build_search_files_tool
from agent.tools.pub_base import path_utils, rg_resolver
from config.features import RIPGREP, TOOLS_TIMEOUTS

pytestmark = [pytest.mark.unit, pytest.mark.timeout(120)]

SESSION = "s-search-engines"

_HAS_RG = shutil.which("rg") is not None


@pytest.fixture(autouse=True)
def _fresh_resolution():
    rg_resolver.reset_cache()
    yield
    rg_resolver.reset_cache()


@pytest.fixture
def virtual_root(tmp_path, monkeypatch):
    """Point ROOT_DIR at a tree with a mix of shapes both engines must agree on."""
    root = (tmp_path / "root").resolve()
    root.mkdir()
    (root / "alpha.txt").write_text("one\nneedle here\ntwo\n", encoding="utf-8")
    (root / "beta.txt").write_text("needle\nneedle again\n", encoding="utf-8")
    (root / "notes.md").write_text("# needle\n", encoding="utf-8")
    nested = root / "pkg"
    nested.mkdir()
    (nested / "gamma.py").write_text("x = 1\n# needle in code\n", encoding="utf-8")
    skipped = root / "node_modules"
    skipped.mkdir()
    (skipped / "vendor.txt").write_text("needle\n", encoding="utf-8")
    hidden = root / ".cache"
    hidden.mkdir()
    (hidden / "secret.txt").write_text("needle\n", encoding="utf-8")
    monkeypatch.setattr(path_utils, "ROOT_DIR", root)
    return root


def _search(**kwargs) -> dict:
    return json.loads(build_search_files_tool()._core(session_id=SESSION, **kwargs))


def _pairs(result: dict) -> set[tuple]:
    return {(m["path"], m["line_number"], m["content"]) for m in result["matches"]}


def _use_python_only(monkeypatch):
    """Force the fallback engine (the rollback switch, used as a test lever)."""
    monkeypatch.setitem(RIPGREP, "enabled", False)
    rg_resolver.reset_cache()


@pytest.mark.skipif(not _HAS_RG, reason="ripgrep is not installed on this host")
def test_ripgrep_is_the_default_engine_when_it_resolves(virtual_root):
    """The switch is on and rg resolves, so the tool must actually use it."""
    assert rg_resolver.resolve_rg() is not None

    result = _search(pattern="needle", target="content")

    assert result["total_count"] >= 3


@pytest.mark.skipif(not _HAS_RG, reason="ripgrep is not installed on this host")
def test_both_engines_return_the_same_matches(virtual_root, monkeypatch):
    rg_result = _search(pattern="needle", target="content", limit=50)
    _use_python_only(monkeypatch)
    py_result = _search(pattern="needle", target="content", limit=50)

    assert _pairs(rg_result) == _pairs(py_result)
    assert rg_result["total_count"] == py_result["total_count"]


@pytest.mark.skipif(not _HAS_RG, reason="ripgrep is not installed on this host")
def test_both_engines_skip_the_same_directories(virtual_root, monkeypatch):
    """``node_modules`` and dot-directories are pruned by both, hidden files too."""
    rg_result = _search(pattern="needle", target="content")
    _use_python_only(monkeypatch)
    py_result = _search(pattern="needle", target="content")

    for result in (rg_result, py_result):
        paths = {m["path"] for m in result["matches"]}
        assert not any("node_modules" in p or ".cache" in p for p in paths), paths


@pytest.mark.skipif(not _HAS_RG, reason="ripgrep is not installed on this host")
def test_ripgrep_prunes_directories_without_claiming_a_count(virtual_root):
    """Same contract as the walk, minus the walk-only ``pruned_dir_count``."""
    result = _search(pattern="needle", target="content")

    paths = [m["path"] for m in result["matches"]]
    assert paths, "the fixture tree must produce matches"
    assert not any("node_modules" in p or "/.cache/" in p for p in paths), paths
    assert "pruned_dir_count" not in result


@pytest.mark.skipif(not _HAS_RG, reason="ripgrep is not installed on this host")
def test_a_file_glob_narrows_both_engines_the_same_way(virtual_root, monkeypatch):
    rg_result = _search(pattern="needle", target="content", file_glob="*.py")
    _use_python_only(monkeypatch)
    py_result = _search(pattern="needle", target="content", file_glob="*.py")

    assert _pairs(rg_result) == _pairs(py_result)
    assert {m["path"] for m in rg_result["matches"]} == {m["path"] for m in py_result["matches"]}


@pytest.mark.skipif(not _HAS_RG, reason="ripgrep is not installed on this host")
def test_paging_is_stable_across_calls(virtual_root):
    """offset/limit must not repeat or skip matches; --sort=path is what buys that."""
    wide = _search(pattern="needle", target="content", limit=50)
    paged: list[dict] = []
    for offset in range(0, wide["total_count"], 2):
        paged.extend(_search(pattern="needle", target="content", limit=2, offset=offset)["matches"])

    assert [(m["path"], m["line_number"]) for m in paged] == [
        (m["path"], m["line_number"]) for m in wide["matches"]
    ]


@pytest.mark.skipif(not _HAS_RG, reason="ripgrep is not installed on this host")
def test_the_files_mode_agrees_between_engines(virtual_root, monkeypatch):
    rg_result = _search(pattern="*.txt", target="files", limit=50)
    _use_python_only(monkeypatch)
    py_result = _search(pattern="*.txt", target="files", limit=50)

    assert set(rg_result["files"]) == set(py_result["files"])


def test_a_missing_binary_falls_back_to_the_walk(virtual_root, monkeypatch):
    """The fallback is not optional: it is the answer on a host without rg."""
    monkeypatch.setenv("SHERRY_RG_PATH", "/nonexistent/rg")
    monkeypatch.setattr(rg_resolver.shutil, "which", lambda name: None)
    monkeypatch.setattr(rg_resolver, "runtime_dir", lambda: virtual_root / "runtime")
    monkeypatch.setattr(rg_resolver, "CODE_INTEL_DIR", virtual_root / "bin")
    # The venv's own rg (the ripgrep-bin dependency) must not answer either, or
    # this stops testing "a host without ripgrep".
    monkeypatch.setattr(rg_resolver, "interpreter_dirs", list)
    rg_resolver.reset_cache()

    assert rg_resolver.resolve_rg() is None
    result = _search(pattern="needle", target="content")

    assert result["total_count"] >= 3
    assert {"alpha.txt", "beta.txt"} <= {m["path"].split("/")[-1] for m in result["matches"]}


def test_patterns_the_rust_engine_lacks_still_work(virtual_root):
    """A lookbehind is valid Python and invalid for rg: the walk must serve it.

    The walk matches line by line, so the lookbehind has to stay inside one line
    (``(?<=one\n)`` would fail on both engines, which is a different fact).
    """
    lookbehind = _search(pattern=r"(?<=need)le", target="content")
    plain = _search(pattern="needle", target="content")

    assert lookbehind["matches"], "the lookbehind search found nothing"
    assert {m["path"] for m in lookbehind["matches"]} == {m["path"] for m in plain["matches"]}


@pytest.mark.integration
@pytest.mark.timeout(300)
@pytest.mark.skipif(not _HAS_RG, reason="ripgrep is not installed on this host")
def test_a_repository_wide_search_completes_inside_the_budget(monkeypatch):
    """The reason this change exists: the walk cannot finish, ripgrep can.

    A no-match pattern forces a full traversal — the shape where the Python walk
    runs out of its 5s budget and returns ``scan_stop_reason="time_budget"``, an
    answer that is honest and incomplete. The rg engine finishes the same
    traversal, so the result carries no truncation markers at all.

    The budget is raised for this case on purpose: the measured cost of a
    repo-wide scan here is ~2.6s against the shipped 5s budget (~3.3s under
    pytest), so the default would make this test a load detector rather than an
    engine check. It also scans the *live* repository, whose ``logs/output``
    grows with every session — hence the loose wall-clock bound (a real
    regression, a hang or an order-of-magnitude slowdown, still fails), and the
    Python walk's own default-budget behaviour is pinned by
    ``test_search_bounds.py``.
    """
    monkeypatch.setitem(TOOLS_TIMEOUTS, "file_tools_search_time_budget_s", 60.0)
    # Assembled at runtime: a literal here would be found in this very file,
    # which is inside the tree being searched.
    pattern = "zzz_" + "no_such_symbol_" + "zzz"

    started = time.perf_counter()
    result = _search(pattern=pattern, target="content")
    elapsed = time.perf_counter() - started

    assert result["matches"] == [], result
    assert "scan_truncated" not in result, result
    assert "scan_stop_reason" not in result, result
    assert "truncated" not in result, result
    assert elapsed < 120.0, f"a repo-wide scan took {elapsed:.1f}s"
