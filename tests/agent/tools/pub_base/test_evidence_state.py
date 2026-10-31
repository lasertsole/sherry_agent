"""B': verification_state changed_paths + evidence modified_files snapshots.

The old staleness rule is a command substring. These tests pin the new
file-set rule: edits accumulate per session, each verification snapshots the
set, and a later write to a snapshotted file invalidates the evidence even when
the command never names it. Legacy rows keep the substring fallback.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agent.tools.pub_base.evidence_ledger import EvidenceLedger
from agent.tools.pub_base.evidence_recorder import (
    mark_evidence_stale,
    record_verification_evidence,
)
from agent.tools.taskflow.evidence_collector import collect_evidence_summary
from config.features import EVIDENCE_LEDGER

pytestmark = [pytest.mark.unit]

A = "agent:main:subagent:state-a"
B = "agent:main:subagent:state-b"
COMMAND = "pytest test_beta.py"


@pytest.fixture(autouse=True)
def ledger(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> EvidenceLedger:
    monkeypatch.setattr(EvidenceLedger, "LEDGER_PATH", str(tmp_path / "evidence.db"))
    monkeypatch.setitem(EVIDENCE_LEDGER, "changed_paths_limit", 200)
    return EvidenceLedger


def _evidence(session: str = A, command: str = COMMAND) -> None:
    record_verification_evidence(command, "1 passed", session)


def _stale(session: str, path: str) -> None:
    mark_evidence_stale(path, session)


def test_changed_paths_accumulate():
    EvidenceLedger.record_file_edit("a.py", A)
    EvidenceLedger.record_file_edit("b.py", A)

    assert EvidenceLedger.changed_paths(A) == ["a.py", "b.py"]


def test_changed_paths_cap_keeps_newest(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setitem(EVIDENCE_LEDGER, "changed_paths_limit", 2)
    EvidenceLedger.record_file_edit("a.py", A)
    EvidenceLedger.record_file_edit("b.py", A)
    EvidenceLedger.record_file_edit("c.py", A)

    assert EvidenceLedger.changed_paths(A) == ["b.py", "c.py"]


def test_a_verification_snapshots_the_session_changed_paths():
    EvidenceLedger.record_file_edit("alpha.py", A)
    _evidence()

    row = EvidenceLedger.read_for_session(A)[0]
    assert row["modified_files"] == ["alpha.py"]


def test_multiple_verifications_share_the_same_snapshot():
    """A lint run must not consume the batch before the test runs."""
    EvidenceLedger.record_file_edit("alpha.py", A)
    _evidence(command="ruff check alpha.py")
    _evidence()

    rows = [row for row in EvidenceLedger.read_for_session(A) if not row.get("event")]
    assert [row["modified_files"] for row in rows] == [["alpha.py"], ["alpha.py"]]


def test_a_cross_file_write_marks_the_snapshot_stale():
    EvidenceLedger.record_file_edit("alpha.py", A)
    _evidence()
    assert "[stale]" not in (collect_evidence_summary(session_key=A) or "")

    _stale(B, "alpha.py")

    summary = collect_evidence_summary(session_key=A) or ""
    assert "[stale]" in summary


def test_exact_matching_avoids_the_substring_false_positive():
    """``feature.py`` is a substring of ``test_feature.py`` but is not in the set."""
    EvidenceLedger.record_file_edit("other.py", A)
    _evidence(command="pytest test_feature.py")

    _stale(B, "feature.py")

    assert "[stale]" not in (collect_evidence_summary(session_key=A) or "")


def test_a_new_row_still_notices_a_write_to_its_test_file():
    EvidenceLedger.record_file_edit("other.py", A)
    _evidence(command="pytest tests/test_feature.py")

    _stale(B, "tests/test_feature.py")

    assert "[stale]" in (collect_evidence_summary(session_key=A) or "")


def test_a_legacy_row_still_uses_the_command_substring():
    EvidenceLedger.for_session(A).append(kind="test", command="pytest alpha.py", status="passed")

    _stale(B, "alpha.py")

    assert "[stale]" in (collect_evidence_summary(session_key=A) or "")


def test_purge_session_removes_verification_state():
    EvidenceLedger.record_file_edit("alpha.py", A)

    EvidenceLedger.purge_session(A)

    assert EvidenceLedger.changed_paths(A) == []
