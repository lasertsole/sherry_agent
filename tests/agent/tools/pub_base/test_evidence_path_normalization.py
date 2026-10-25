"""Path normalization for evidence: same file, different spellings."""

from __future__ import annotations

from pathlib import Path

import pytest

from agent.tools.pub_base.evidence_ledger import EvidenceLedger, normalize_evidence_path
from agent.tools.pub_base.evidence_recorder import (
    mark_evidence_stale,
    record_verification_evidence,
)
from agent.tools.taskflow.evidence_collector import collect_evidence_summary
from runtime.session.state_keys import StateKey
from runtime.session.state_register import state_register_mem

pytestmark = [pytest.mark.unit]

A = "agent:main:subagent:path-a"
B = "agent:main:subagent:path-b"


@pytest.fixture(autouse=True)
def ledger(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> EvidenceLedger:
    monkeypatch.setattr(EvidenceLedger, "LEDGER_PATH", str(tmp_path / "evidence.db"))
    yield
    for key in (A, B):
        state_register_mem.clear_session(key)


@pytest.fixture()
def root(tmp_path: Path) -> Path:
    project = tmp_path / "project"
    (project / "src").mkdir(parents=True)
    for key in (A, B):
        state_register_mem.set_state(key, StateKey.PROJECT_DIR, str(project))
    return project


def test_dot_dotdot_and_absolute_inside_root_normalize_to_relative(root: Path):
    absolute = str((root / "src" / "alpha.py").resolve())

    EvidenceLedger.record_file_edit("./src/alpha.py", A)
    EvidenceLedger.record_file_edit("src/../src/alpha.py", A)
    EvidenceLedger.record_file_edit(absolute, A)

    assert EvidenceLedger.changed_paths(A) == ["src/alpha.py"]


def test_an_absolute_path_outside_the_root_stays_absolute(root: Path, tmp_path: Path):
    outside = tmp_path / "outside.py"

    EvidenceLedger.record_file_edit(str(outside), A)

    assert EvidenceLedger.changed_paths(A) == [outside.resolve().as_posix()]


def test_cross_session_absolute_write_matches_a_relative_snapshot(root: Path):
    EvidenceLedger.record_file_edit("src/alpha.py", A)
    record_verification_evidence("pytest test_beta.py", "1 passed", A)

    mark_evidence_stale(str((root / "src" / "alpha.py").resolve()), B)

    summary = collect_evidence_summary(session_key=A) or ""
    assert "[stale]" in summary
    stale = EvidenceLedger.read_for_session(A)[-1]
    assert stale["file_path"] == "src/alpha.py"


def test_without_a_session_root_only_string_normalization_happens():
    EvidenceLedger.record_file_edit("./a/../b.py", "unbound-session")

    assert EvidenceLedger.changed_paths("unbound-session") == ["b.py"]


def test_normalize_is_idempotent(root: Path):
    once = normalize_evidence_path("./src/alpha.py", A)
    assert normalize_evidence_path(once, A) == once
