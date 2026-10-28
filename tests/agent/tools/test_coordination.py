"""Cross-agent coordination: path claims and the peer findings board.

Every case here comes from the plan's condition-path checklist — the store's
missing-directory / TTL / concurrency / normalization branches, each tool's
disabled / empty / over-cap / out-of-project / too-long branches, and the
write-side advisory's "never a precondition" invariant.
"""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

import pytest

import agent.tools.pub_base.coordination as store
from agent.tools.pub_base import coordination as coord
from config.features import COORDINATION

pytestmark = [pytest.mark.unit, pytest.mark.timeout(120)]


@pytest.fixture()
def project(tmp_path: Path) -> Path:
    """A project that already has .sherry/ (created by WorkspaceNotice)."""
    root = tmp_path / "proj"
    (root / ".sherry").mkdir(parents=True)
    return root


@pytest.fixture()
def features(monkeypatch: pytest.MonkeyPatch):
    """Both switches ON with the shipped limits (each test may narrow them)."""
    monkeypatch.setitem(COORDINATION, "claims_enabled", True)
    monkeypatch.setitem(COORDINATION, "findings_enabled", True)
    monkeypatch.setitem(COORDINATION, "claim_ttl_seconds", 1800)
    monkeypatch.setitem(COORDINATION, "max_paths_per_claim", 50)
    monkeypatch.setitem(COORDINATION, "max_findings_per_query", 20)
    monkeypatch.setitem(COORDINATION, "max_text_chars", 2000)
    return COORDINATION


# ── storage layer ────────────────────────────────────────────────────────────


class TestStoreBranches:
    def test_a_project_without_sherry_answers_empty_and_creates_nothing(self, tmp_path):
        bare = tmp_path / "bare"
        bare.mkdir()

        assert coord.claim_paths(bare, "s1", ["a.py"]) == 0
        assert coord.active_claims(bare, ["a.py"]) == []
        assert coord.record_finding(bare, "s1", "FACT", "x") is None
        assert coord.search_findings(bare) == []
        assert not (bare / ".sherry").exists(), "a read must not litter the project"

    def test_reclaiming_the_same_path_updates_instead_of_duplicating(self, project):
        coord.claim_paths(project, "s1", ["a.py"], note="first")
        coord.claim_paths(project, "s1", ["a.py"], note="second")

        rows = coord.active_claims(project, ["a.py"])

        assert len(rows) == 1 and rows[0].note == "second"

    def test_an_expired_claim_is_pruned(self, project, monkeypatch):
        real_time = time.time
        monkeypatch.setattr(store.time, "time", lambda: real_time() - 10_000)
        coord.claim_paths(project, "s1", ["a.py"], ttl_seconds=1)
        monkeypatch.undo()

        assert coord.active_claims(project, ["a.py"]) == []
        coord.claim_paths(project, "s2", ["b.py"])  # a write sweeps the expired row

        conn = sqlite3.connect(str(coord.coordination_db_path(project)))
        try:
            assert conn.execute("SELECT COUNT(*) FROM claims").fetchone()[0] == 1
        finally:
            conn.close()

    def test_release_only_drops_the_calling_sessions_claims(self, project):
        coord.claim_paths(project, "s1", ["a.py", "b.py"])
        coord.claim_paths(project, "s2", ["a.py"])

        assert coord.release_claims(project, "s1") == 2
        assert coord.active_claims(project, ["a.py"]) == [
            row for row in coord.active_claims(project, ["a.py"]) if row.session_id == "s2"
        ]

    def test_two_sessions_see_each_other_regardless_of_order(self, project):
        coord.claim_paths(project, "s1", ["shared.py"])
        coord.claim_paths(project, "s2", ["shared.py"])

        assert [row.session_id for row in coord.active_claims(project, ["shared.py"])] == [
            "s1",
            "s2",
        ]
        assert [
            row.session_id
            for row in coord.active_claims(project, ["shared.py"], exclude_session="s1")
        ] == ["s2"]

    def test_paths_are_compared_after_normalization(self, project):
        coord.claim_paths(project, "s1", ["a.py"])

        assert coord.active_claims(project, ["./a.py"]) == []
        # The STORE compares what it is given; normalization is the tool's job —
        # pinned here so the split stays explicit.
        assert [row.path for row in coord.active_claims(project, ["a.py"])] == ["a.py"]

    def test_the_store_sets_a_busy_timeout(self, project):
        """The PRAGMA is per connection, so it must be on the store's own."""
        conn = store._connect(project)
        assert conn is not None
        try:
            assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == store._BUSY_TIMEOUT_MS
        finally:
            conn.close()


# ── tool layer ───────────────────────────────────────────────────────────────


@pytest.fixture()
def wired(project, monkeypatch):
    """Bind every tool call to ``project`` (the session→root seam)."""
    import agent.tools.coordination_tools as tools

    monkeypatch.setattr(tools, "_project_root", lambda _sid: project)
    return project


class TestToolBranches:
    def test_disabled_switches_remove_the_tools_entirely(self, monkeypatch):
        from agent.tools.coordination_tools import build_coordination_tools

        monkeypatch.setitem(COORDINATION, "claims_enabled", False)
        monkeypatch.setitem(COORDINATION, "findings_enabled", False)

        assert build_coordination_tools() == []

    def test_claims_only_and_findings_only_shapes(self, monkeypatch):
        from agent.tools.coordination_tools import build_coordination_tools

        monkeypatch.setitem(COORDINATION, "claims_enabled", True)
        monkeypatch.setitem(COORDINATION, "findings_enabled", False)
        assert [t.name for t in build_coordination_tools()] == ["claim_scope"]

        monkeypatch.setitem(COORDINATION, "claims_enabled", False)
        monkeypatch.setitem(COORDINATION, "findings_enabled", True)
        assert [t.name for t in build_coordination_tools()] == ["share_finding", "peers_findings"]

    def test_a_disabled_tool_refuses_without_touching_the_store(self, wired: Path, monkeypatch):
        from agent.tools.coordination_tools import claim_scope

        monkeypatch.setitem(COORDINATION, "claims_enabled", False)

        out = claim_scope.invoke({"paths": ["a.py"], "session_id": "s1"})

        assert "disabled" in out
        assert not coord.coordination_db_path(wired).exists()

    def test_an_empty_path_list_is_a_usage_message(self, wired: Path, features):
        from agent.tools.coordination_tools import claim_scope

        out = claim_scope.invoke({"paths": [], "session_id": "s1"})

        assert "at least one path" in out

    def test_the_path_cap_truncates_and_says_so(self, wired: Path, features, monkeypatch):
        from agent.tools.coordination_tools import claim_scope

        monkeypatch.setitem(COORDINATION, "max_paths_per_claim", 2)

        out = claim_scope.invoke({"paths": ["a.py", "b.py", "c.py", "d.py"], "session_id": "s1"})

        assert "claimed 2 path(s)" in out and "2 dropped" in out

    def test_out_of_project_paths_are_skipped_and_counted(self, wired: Path, features):
        from agent.tools.coordination_tools import claim_scope

        out = claim_scope.invoke({"paths": ["/etc/passwd", "../outside.py"], "session_id": "s1"})

        assert "none of the given paths" in out

    def test_a_mixed_list_keeps_the_inside_paths(self, wired: Path, features):
        from agent.tools.coordination_tools import claim_scope

        out = claim_scope.invoke({"paths": ["a.py", "/etc/passwd"], "session_id": "s1"})

        assert "claimed 1 path(s)" in out and "skipped" in out
        assert [row.path for row in coord.active_claims(wired, ["a.py"])] == ["a.py"]

    def test_a_long_note_is_truncated(self, wired: Path, features, monkeypatch):
        from agent.tools.coordination_tools import claim_scope

        monkeypatch.setitem(COORDINATION, "max_text_chars", 10)
        claim_scope.invoke({"paths": ["a.py"], "note": "x" * 100, "session_id": "s1"})

        assert len(coord.active_claims(wired, ["a.py"])[0].note) == 10

    def test_an_invalid_finding_kind_is_refused_without_writing(self, wired: Path, features):
        """The Literal in the schema rejects it before the body runs."""
        from pydantic import ValidationError

        from agent.tools.coordination_tools import share_finding

        with pytest.raises(ValidationError):
            share_finding.invoke({"kind": "NONSENSE", "text": "x", "session_id": "s1"})

        assert coord.search_findings(wired) == []

    def test_finding_text_is_capped(self, wired: Path, features, monkeypatch):
        from agent.tools.coordination_tools import share_finding

        monkeypatch.setitem(COORDINATION, "max_text_chars", 12)
        share_finding.invoke({"kind": "FACT", "text": "y" * 200, "session_id": "s1"})

        assert len(coord.search_findings(wired)[0].text) == 12

    def test_findings_query_is_and_semantics(self, wired: Path, features):
        from agent.tools.coordination_tools import peers_findings, share_finding

        share_finding.invoke(
            {"kind": "FAIL", "text": "redis cache breaks the parser", "session_id": "s1"}
        )
        share_finding.invoke({"kind": "FACT", "text": "the parser is stateful", "session_id": "s2"})

        out = peers_findings.invoke({"query": "parser redis", "session_id": "s3"})

        assert "redis cache breaks" in out and "stateful" not in out

    def test_findings_query_can_filter_by_kind(self, wired: Path, features):
        from agent.tools.coordination_tools import peers_findings, share_finding

        share_finding.invoke({"kind": "FAIL", "text": "alpha", "session_id": "s1"})
        share_finding.invoke({"kind": "FACT", "text": "alpha", "session_id": "s1"})

        out = peers_findings.invoke({"query": "alpha", "kind": "FACT", "session_id": "s2"})

        assert "FACT" in out and "FAIL" not in out

    def test_the_query_limit_is_clamped(self, wired: Path, features, monkeypatch):
        from agent.tools.coordination_tools import peers_findings, share_finding

        monkeypatch.setitem(COORDINATION, "max_findings_per_query", 2)
        for index in range(5):
            share_finding.invoke({"kind": "FACT", "text": f"note {index}", "session_id": "s1"})

        out = peers_findings.invoke({"query": "note", "limit": 99, "session_id": "s2"})

        assert out.count("- FACT") == 2

    def test_an_empty_board_answers_so(self, wired: Path, features):
        from agent.tools.coordination_tools import peers_findings

        assert "no matching findings" in peers_findings.invoke({"session_id": "s1"})

    def test_reading_never_writes(self, wired: Path, features):
        from agent.tools.coordination_tools import peers_findings, share_finding

        share_finding.invoke({"kind": "FACT", "text": "exists", "session_id": "s1"})
        before = _row_count(wired, "findings")

        peers_findings.invoke({"query": "exists", "session_id": "s2"})

        assert _row_count(wired, "findings") == before

    def test_findings_disabled_removes_its_tools(self, wired: Path, monkeypatch):
        from agent.tools.coordination_tools import peers_findings, share_finding

        monkeypatch.setitem(COORDINATION, "findings_enabled", False)

        assert "disabled" in share_finding.invoke({"kind": "FACT", "text": "x", "session_id": "s1"})
        assert "disabled" in peers_findings.invoke({"session_id": "s1"})


# ── write-side advisory ──────────────────────────────────────────────────────


def _write(project: Path, session: str, path: str, text: str = "x") -> dict:
    """Run the REAL write_file tool against the project."""
    from agent.tools.file_tools.write_file import build_write_file_tool

    tool = build_write_file_tool()
    out = tool.invoke({"file_path": path, "text": text, "session_id": session})
    return json.loads(out)


class TestWriteSideAdvisory:
    @pytest.fixture()
    def bound(self, project: Path, monkeypatch):
        """Bind the write tools' session→root resolution to the project."""
        import agent.tools.pub_base.path_utils as path_utils
        import agent.tools.coordination_tools as tools
        import runtime.session.project_dir as project_dir_mod

        monkeypatch.setattr(tools, "_project_root", lambda _sid: project)
        monkeypatch.setattr(path_utils, "current_project_dir", lambda _sid: project)
        monkeypatch.setattr(project_dir_mod, "current_project_dir", lambda _sid: project)
        return project

    def test_a_peer_claim_adds_one_advisory_line(self, bound: Path, features, monkeypatch):
        monkeypatch.setattr(
            "agent.tools.file_tools.write_file.resolve_tool_session_id", lambda sid, rm: sid
        )
        coord.claim_paths(bound, "peer", ["a.py"], note="refactoring")

        result = _write(bound, "s1", "a.py")

        assert result["success"] is True
        assert "claim_notice" in result
        assert "also claimed by" in result["claim_notice"]

    def test_the_sessions_own_claim_is_silent(self, bound: Path, features, monkeypatch):
        monkeypatch.setattr(
            "agent.tools.file_tools.write_file.resolve_tool_session_id", lambda sid, rm: sid
        )
        coord.claim_paths(bound, "s1", ["a.py"])

        assert "claim_notice" not in _write(bound, "s1", "a.py")

    def test_the_feature_being_off_never_queries_the_store(self, bound: Path, monkeypatch):
        monkeypatch.setattr(
            "agent.tools.file_tools.write_file.resolve_tool_session_id", lambda sid, rm: sid
        )
        monkeypatch.setitem(COORDINATION, "claims_enabled", False)
        calls: list[int] = []
        monkeypatch.setattr(coord, "active_claims", lambda *a, **k: calls.append(1) or [])

        assert "claim_notice" not in _write(bound, "s1", "a.py")
        assert calls == []

    def test_a_broken_store_still_writes(self, bound: Path, features, monkeypatch):
        monkeypatch.setattr(
            "agent.tools.file_tools.write_file.resolve_tool_session_id", lambda sid, rm: sid
        )

        def _boom(*_args, **_kwargs):
            raise RuntimeError("store exploded")

        monkeypatch.setattr(coord, "active_claims", _boom)

        result = _write(bound, "s1", "a.py")

        assert result["success"] is True and "claim_notice" not in result

    def test_the_notice_fires_once_per_path_in_the_window(self, bound: Path, features, monkeypatch):
        monkeypatch.setattr(
            "agent.tools.file_tools.write_file.resolve_tool_session_id", lambda sid, rm: sid
        )
        import agent.tools.coordination_tools as tools

        tools._CLAIM_NOTICE_SEEN.clear()
        coord.claim_paths(bound, "peer", ["a.py"])

        first = _write(bound, "s1", "a.py")
        second = _write(bound, "s1", "a.py")

        assert "claim_notice" in first
        assert "claim_notice" not in second, "the same path stays quiet inside the window"


class TestMergeFinding:
    def test_a_merge_publishes_a_patch_finding_when_enabled(self, features):
        from agent.tools.subagent.announce.workspace_merge import _share_merge_finding
        from agent.tools.subagent.isolation.merge import MergeReport

        recorded: list[tuple] = []

        def _spy(root, session, files, text):
            recorded.append((session, tuple(files), text))

        import agent.tools.coordination_tools as tools

        original = tools.share_merge_finding
        tools.share_merge_finding = _spy
        try:
            report = MergeReport(parent_root=Path("/proj"), applied=["utils.py"])
            _share_merge_finding(_stub_run(), report)
        finally:
            tools.share_merge_finding = original

        assert recorded and recorded[0][1] == ("utils.py",)
        assert "no interface changes" in recorded[0][2]

    def test_a_merge_records_nothing_when_disabled(self, monkeypatch):
        from agent.tools.subagent.announce.workspace_merge import _share_merge_finding
        from agent.tools.subagent.isolation.merge import MergeReport

        monkeypatch.setitem(COORDINATION, "findings_enabled", False)
        calls: list[int] = []
        import agent.tools.coordination_tools as tools

        original = tools.share_merge_finding
        tools.share_merge_finding = lambda *a, **k: calls.append(1)
        try:
            _share_merge_finding(_stub_run(), MergeReport(parent_root=Path("/proj")))
        finally:
            tools.share_merge_finding = original

        assert calls == []


class TestDefaultFaceIsUnchanged:
    def test_both_switches_off_keeps_the_tool_list_identical(self, monkeypatch):
        """The default deployment must not gain a single tool."""
        from agent.tools import build_main_tools

        monkeypatch.setitem(COORDINATION, "claims_enabled", False)
        monkeypatch.setitem(COORDINATION, "findings_enabled", False)

        names = {tool.name for tool in build_main_tools()}

        assert not names & {"claim_scope", "share_finding", "peers_findings"}


def _row_count(project: Path, table: str) -> int:
    conn = sqlite3.connect(str(coord.coordination_db_path(project)))
    try:
        return int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
    finally:
        conn.close()


def _stub_run():
    from agent.tools.subagent.types.registry import SubagentRunRecord

    return SubagentRunRecord(
        run_id="run-coord-1",
        child_session_key="agent:main:subagent:child",
        requester_session_key="agent:main:session:owner",
        spawned_by="agent:main:session:owner",
        task="probe",
    )
