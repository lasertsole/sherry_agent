"""Tests for the evidence summary collector used by judge prompts."""

from __future__ import annotations

import pytest

from agent.tools.todolist.evidence_ledger import EvidenceLedger
from agent.tools.taskflow.evidence_collector import collect_evidence_summary

pytestmark = [pytest.mark.unit]


@pytest.fixture
def ledger_path(tmp_path, monkeypatch):
    path = tmp_path / "evidence.jsonl"
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


def test_flow_id_used_as_scope(ledger_path):
    EvidenceLedger.for_session("flow-9").append(kind="test", command="pytest -q", status="passed")

    summary = collect_evidence_summary(flow_id="flow-9")

    assert summary is not None
    assert "pytest -q" in summary
