"""The ripgrep engine's own contract, exercised against a scripted rg.

Real ripgrep cannot be asked to hang, to exit 2, or to flood its stderr on
demand — and those are exactly the paths that decide whether a search is
*correct* (a hard error must fall back, never present partial as complete) and
whether it can *hang* (a full stderr pipe with the parent blocked on stdout).
So the behaviours live here against a fake `rg` whose every branch is scripted,
while `tests/agent/tools/file_tools/test_search_engines.py` proves the real
binary agrees with the Python walk.
"""

from __future__ import annotations

import json
import stat
import textwrap

import pytest

from agent.tools.file_tools.search_scan import ScanState, SearchQuery
from agent.tools.pub_base import rg_backend, rg_resolver

pytestmark = [pytest.mark.unit, pytest.mark.timeout(120)]

_FAKE = textwrap.dedent(
    '''
    """Scripted stand-in for ripgrep; behaviour comes from FAKE_RG_MODE."""
    import json
    import os
    import sys
    import time


    def frame(kind, path, line=None, text=None):
        data = {"path": {"text": path}}
        if line is not None:
            data["line_number"] = line
        if text is not None:
            data["lines"] = {"text": text}
        sys.stdout.write(json.dumps({"type": kind, "data": data}) + "\\n")


    def emit_matches():
        frame("begin", "b.txt")
        frame("match", "b.txt", 4, "second file line\\n")
        frame("end", "b.txt")
        frame("begin", "a.txt")
        frame("match", "a.txt", 2, "first hit\\n")
        frame("match", "a.txt", 7, "second hit\\n")
        sys.stdout.write(json.dumps({"type": "summary", "data": {}}) + "\\n")
        frame("end", "a.txt")


    def main():
        mode = os.environ.get("FAKE_RG_MODE", "ok")
        argv = sys.argv[1:]

        if "--version" in argv:
            print("ripgrep 14.1.1 (fake)")
            return 0

        with open(os.environ["FAKE_RG_ARGV_LOG"], "a", encoding="utf-8") as fh:
            fh.write(json.dumps(argv) + "\\n")

        if mode == "hard_error":
            sys.stderr.write("regex parse error: unclosed group\\n")
            return 2

        if mode == "stderr_flood":
            # A full stderr pipe would stall the child while we block on stdout.
            sys.stderr.write("x" * (2 * 1024 * 1024))
            sys.stderr.flush()
            return 2

        if mode == "slow_partial":
            frame("match", "slow.txt", 1, "one match before the hang\\n")
            sys.stdout.flush()
            time.sleep(60)
            return 0

        if mode == "slow_empty":
            time.sleep(60)
            return 0

        if mode == "nomatch":
            return 1

        emit_matches()
        return 0


    raise SystemExit(main())
    '''
)


@pytest.fixture
def fake_rg(tmp_path, monkeypatch):
    """Install a scripted rg and return a setter for its mode."""
    argv_log = tmp_path / "argv.jsonl"
    # One self-contained executable: the shebang matters, because the resolver
    # execs the candidate directly (a script without one fails the probe and the
    # resolver would quietly fall through to whatever rg the host happens to have).
    binary = tmp_path / "rg"
    binary.write_text("#!/usr/bin/env python3\n" + _FAKE, encoding="utf-8")
    binary.chmod(binary.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

    monkeypatch.setenv("SHERRY_RG_PATH", str(binary))
    monkeypatch.setenv("FAKE_RG_ARGV_LOG", str(argv_log))
    monkeypatch.setenv("FAKE_RG_MODE", "ok")
    rg_resolver.reset_cache()

    def set_mode(mode: str) -> None:
        monkeypatch.setenv("FAKE_RG_MODE", mode)

    yield set_mode, argv_log
    rg_resolver.reset_cache()


@pytest.fixture
def tree(tmp_path):
    """A search root with two text files, so matches resolve to real paths."""
    root = tmp_path / "root"
    root.mkdir()
    (root / "a.txt").write_text("alpha\nfirst hit\nbeta\n", encoding="utf-8")
    (root / "b.txt").write_text("x\ny\nz\nsecond file line\n", encoding="utf-8")
    return root


def _state(offset: int = 0, limit: int = 50) -> ScanState:
    return ScanState.start(offset, limit)


def _binary() -> str:
    """The fake installed by the fixture, resolved the way production resolves it."""
    resolved = rg_resolver.resolve_rg()
    assert resolved is not None, "the fixture's fake rg was not picked up"
    return resolved


def test_matches_are_parsed_into_the_walk_shape(fake_rg, tree):
    set_mode, _ = fake_rg
    set_mode("ok")

    result = rg_backend.rg_search(SearchQuery("hit", tree), _state(), binary=_binary())

    assert result is not None
    assert [m["path"] for m in result["matches"]] == [m["path"] for m in result["matches"]]
    lines = [m["line_number"] for m in result["matches"]]
    assert lines == [4, 2, 7]
    assert result["matches"][0]["content"] == "second file line"
    assert result["total_count"] == 3
    assert "truncated" not in result


def test_a_match_line_is_truncated_like_the_walk(fake_rg, tree):
    set_mode, _ = fake_rg
    set_mode("ok")

    result = rg_backend.rg_search(SearchQuery("hit", tree), _state(), binary=_binary())

    assert all(len(m["content"]) <= 500 for m in result["matches"])


def test_no_match_is_a_complete_scan_not_a_truncated_one(fake_rg, tree):
    """The difference this whole change exists for: empty and *complete*."""
    set_mode, _ = fake_rg
    set_mode("nomatch")

    result = rg_backend.rg_search(SearchQuery("nothing", tree), _state(), binary=_binary())

    assert result is not None
    assert result["matches"] == []
    assert "scan_truncated" not in result
    assert "truncated" not in result


def test_a_hard_error_falls_back_instead_of_reporting_partial(fake_rg, tree):
    set_mode, _ = fake_rg
    set_mode("hard_error")

    assert rg_backend.rg_search(SearchQuery("hit", tree), _state(), binary=_binary()) is None


def test_a_flooded_stderr_pipe_does_not_deadlock(fake_rg, tree):
    """2 MB on stderr, unread: without a drain thread the child blocks and we hang."""
    set_mode, _ = fake_rg
    set_mode("stderr_flood")

    assert rg_backend.rg_search(SearchQuery("hit", tree), _state(), binary=_binary()) is None


def test_the_watchdog_returns_partial_results_flagged_truncated(fake_rg, tree, monkeypatch):
    set_mode, _ = fake_rg
    set_mode("slow_partial")
    monkeypatch.setitem(rg_backend.TOOLS_TIMEOUTS, "file_tools_search_time_budget_s", 0.5)
    monkeypatch.setitem(rg_backend.RIPGREP, "terminate_grace_seconds", 0.5)
    monkeypatch.setitem(rg_backend.RIPGREP, "kill_grace_seconds", 0.5)

    result = rg_backend.rg_search(SearchQuery("hit", tree), _state(), binary=_binary())

    assert result is not None, "a hung child must not force the Python fallback"
    assert result["scan_stop_reason"] == "time_budget"
    assert result["scan_truncated"] is True
    assert result["matches"], "the match collected before the hang must survive"


def test_the_watchdog_bounds_a_child_that_produces_nothing(fake_rg, tree, monkeypatch):
    set_mode, _ = fake_rg
    set_mode("slow_empty")
    monkeypatch.setitem(rg_backend.TOOLS_TIMEOUTS, "file_tools_search_time_budget_s", 0.5)
    monkeypatch.setitem(rg_backend.RIPGREP, "terminate_grace_seconds", 0.5)
    monkeypatch.setitem(rg_backend.RIPGREP, "kill_grace_seconds", 0.5)

    result = rg_backend.rg_search(SearchQuery("hit", tree), _state(), binary=_binary())

    assert result is not None
    assert result["scan_stop_reason"] == "time_budget"
    assert result["matches"] == []


def test_exclusions_travel_as_glob_arguments(fake_rg, tree):
    """Regression: bare `!pattern` tokens were read by rg as the *search pattern*.

    That mistake made rg scan everything, match nothing the caller asked for and
    exit 2 (it treated the globs as non-existent paths), which the fallback then
    hid. The argv is the contract, so it is asserted directly.
    """
    set_mode, argv_log = fake_rg
    set_mode("ok")

    rg_backend.rg_search(SearchQuery("hit", tree), _state(), binary=_binary())

    argv = json.loads(argv_log.read_text(encoding="utf-8").splitlines()[0])
    globs = [a for a in argv if a.startswith("--glob=")]
    assert globs, "exclusions must be --glob=<pattern> arguments"
    assert all(g.startswith("--glob=!") for g in globs)
    assert not [a for a in argv if a.startswith("!")], "no bare glob tokens"
    assert argv[-3:] == ["--", "hit", "."], "pattern and path stay separated by --"


def test_include_globs_precede_the_exclusions(fake_rg, tree):
    """rg resolves overlapping globs last-match-wins.

    Measured: with an exclusion first and ``--glob=*.txt`` after it, files inside
    ``node_modules`` came back — the include re-admitted them. Ordering is the
    contract, so it is asserted.
    """
    set_mode, argv_log = fake_rg
    set_mode("ok")

    rg_backend.rg_search(SearchQuery("hit", tree, file_glob="*.py"), _state(), binary=_binary())

    argv = json.loads(argv_log.read_text(encoding="utf-8").splitlines()[0])
    include_at = argv.index("--glob=*.py")
    first_exclude = next(i for i, a in enumerate(argv) if a.startswith("--glob=!"))
    assert include_at < first_exclude, argv


def test_paging_stops_one_match_past_the_page(fake_rg, tree):
    set_mode, _ = fake_rg
    set_mode("ok")

    result = rg_backend.rg_search(
        SearchQuery("hit", tree), _state(offset=1, limit=1), binary=_binary()
    )

    assert result is not None
    assert len(result["matches"]) == 1
    assert result["truncated"] is True
    assert result["matches"][0]["line_number"] == 2


@pytest.mark.parametrize(
    "pattern",
    ["(?<=x)y", r"(\w)\1", "(?!x)y", "(?<!x)y", "(?>x)y", r"hit"],
)
def test_patterns_the_rust_engine_cannot_express_go_to_python(pattern):
    needs_python = rg_backend.pattern_needs_python(pattern)

    assert needs_python == (pattern != "hit")
