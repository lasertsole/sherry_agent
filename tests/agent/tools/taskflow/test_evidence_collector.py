"""Tests for the evidence summary collector used by judge prompts."""

from __future__ import annotations

import pytest

from agent.tools.pub_base.evidence_ledger import EvidenceLedger
from agent.tools.taskflow.evidence_collector import collect_evidence_summary

pytestmark = [pytest.mark.unit]


@pytest.fixture
def ledger_path(tmp_path, monkeypatch):
    path = tmp_path / "evidence.db"
    monkeypatch.setattr(EvidenceLedger, "LEDGER_PATH", str(path))
    return path


def test_returns_none_without_evidence(ledger_path):
    assert collect_evidence_summary(session_key="s1") is None


def test_summarizes_pass_and_fail(ledger_path):
    ledger = EvidenceLedger.for_session("s1")
    ledger.append(kind="test", command="pytest -q", status="passed")
    ledger.append(kind="lint", command="ruff check .", status="failed")

    summary = collect_evidence_summary(session_key="s1")

    assert summary is not None
    assert "[pass] test: `pytest -q`" in summary
    assert "[FAIL] lint: `ruff check .`" in summary


def test_stale_derived_from_later_event(ledger_path):
    ledger = EvidenceLedger.for_session("s1")
    ledger.append(kind="test", command="pytest foo.py", status="passed")
    ledger.mark_stale_for_path("foo.py")

    summary = collect_evidence_summary(session_key="s1")

    assert summary is not None
    assert "[pass] test: `pytest foo.py` [stale]" in summary


def test_stale_event_before_evidence_does_not_apply(ledger_path):
    ledger = EvidenceLedger.for_session("s1")
    ledger.mark_stale_for_path("foo.py")
    ledger.append(kind="test", command="pytest foo.py", status="passed")

    summary = collect_evidence_summary(session_key="s1")

    assert summary is not None
    assert "[stale]" not in summary


def test_stale_event_with_unrelated_path_does_not_apply(ledger_path):
    ledger = EvidenceLedger.for_session("s1")
    ledger.append(kind="test", command="pytest foo.py", status="passed")
    ledger.mark_stale_for_path("bar.py")

    summary = collect_evidence_summary(session_key="s1")

    assert summary is not None
    assert "[stale]" not in summary


def test_session_scope_isolates_evidence(ledger_path):
    EvidenceLedger.for_session("s1").append(kind="test", command="pytest a.py", status="passed")
    EvidenceLedger.for_session("s2").append(kind="lint", command="ruff b.py", status="passed")

    summary = collect_evidence_summary(session_key="s1")

    assert summary is not None
    assert "pytest a.py" in summary
    assert "ruff b.py" not in summary


class TestMultiKeyScope:
    """A flow's evidence is the AGGREGATE of the sessions that did its work.

    Evidence rows carry a session key, never a flow id, so the flow-level
    consumer passes the session keys of its steps (plus its creator) instead of
    looking a flow up as if it were a session.
    """

    def test_several_keys_are_aggregated(self, ledger_path):
        EvidenceLedger.for_session("child-a").append(
            kind="test", command="pytest a.py", status="passed"
        )
        EvidenceLedger.for_session("child-b").append(
            kind="test", command="pytest b.py", status="failed"
        )

        summary = collect_evidence_summary(session_keys=["child-a", "child-b"])

        assert summary is not None
        assert "pytest a.py" in summary
        assert "pytest b.py" in summary

    def test_unknown_keys_are_skipped_and_all_unknown_is_none(self, ledger_path):
        EvidenceLedger.for_session("child-a").append(
            kind="test", command="pytest a.py", status="passed"
        )

        assert collect_evidence_summary(session_keys=["child-a", "ghost"]) is not None
        assert collect_evidence_summary(session_keys=["ghost", " "]) is None

    def test_staleness_never_crosses_sessions(self, ledger_path):
        """A stale event in one child must not mark another child's row stale."""
        EvidenceLedger.for_session("child-a").append(
            kind="test", command="pytest foo.py", status="passed"
        )
        EvidenceLedger.for_session("child-b").append(
            kind="test", command="pytest foo.py", status="passed"
        )
        EvidenceLedger.for_session("child-b").mark_stale_for_path("foo.py")

        summary = collect_evidence_summary(session_keys=["child-a", "child-b"])

        assert summary is not None
        # Exactly one of the two identical commands is stale (child-b's).
        assert summary.count("pytest foo.py") == 2
        assert summary.count("[stale]") == 1

    def test_one_sql_read_for_all_keys(self, ledger_path, monkeypatch):
        """Keys are aggregated in ONE indexed query, not one read per key."""
        EvidenceLedger.for_session("child-a").append(
            kind="test", command="pytest a.py", status="passed"
        )
        calls: list[list[str]] = []
        real_read_for_keys = EvidenceLedger.read_for_keys.__func__

        def _counting_read_for_keys(cls, keys):
            calls.append(list(keys))
            return real_read_for_keys(cls, keys)

        monkeypatch.setattr(EvidenceLedger, "read_for_keys", classmethod(_counting_read_for_keys))

        assert collect_evidence_summary(session_keys=["child-a", "child-b", "child-c"])

        assert len(calls) == 1
        assert calls[0] == ["child-a", "child-b", "child-c"]
