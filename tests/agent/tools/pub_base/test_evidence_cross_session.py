"""Cross-session evidence staleness propagation.

A file write invalidates the writer's own evidence today. These tests pin the
new broadcast: every OTHER session whose evidence command names the written
path receives its own stale row, so its evidence collector (and the TaskFlow
finish gate) sees the invalidation. The ledger schema is unchanged — the new
row rides the existing ``{"event": "stale"}`` shape with ``scope`` and
``source_session`` metadata.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agent.tools.pub_base.evidence_ledger import EvidenceLedger
from agent.tools.pub_base.evidence_recorder import mark_evidence_stale
from agent.tools.taskflow.evidence_collector import collect_evidence_summary
from config.features import EVIDENCE_CROSS_SESSION_STALENESS

pytestmark = [pytest.mark.unit]

PATH = "tests/test_feature.py"


@pytest.fixture(autouse=True)
def ledger(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> EvidenceLedger:
    monkeypatch.setattr(EvidenceLedger, "LEDGER_PATH", str(tmp_path / "evidence.db"))
    monkeypatch.setitem(EVIDENCE_CROSS_SESSION_STALENESS, "enabled", True)
    monkeypatch.setitem(EVIDENCE_CROSS_SESSION_STALENESS, "max_sessions_scan", 200)
    monkeypatch.setitem(EVIDENCE_CROSS_SESSION_STALENESS, "exclude_self", True)
    return EvidenceLedger


def _evidence(session: str, command: str = f"pytest {PATH}") -> None:
    EvidenceLedger.for_session(session).append(kind="test", command=command, status="passed")


def test_a_write_marks_another_sessions_matching_evidence_stale():
    _evidence("session-a")
    _evidence("session-b", "pytest tests/test_b.py")

    mark_evidence_stale(PATH, "session-b")

    summary = collect_evidence_summary(session_key="session-a")
    assert summary is not None and "[stale]" in summary

    stale = EvidenceLedger.read_for_session("session-a")[-1]
    assert stale["event"] == "stale"
    assert stale["file_path"] == PATH
    assert stale["scope"] == "cross_session"
    assert stale["source_session"] == "session-b"

    # The writer's own session keeps exactly its per-session stale row; the
    # broadcast excludes it.
    writer_rows = EvidenceLedger.read_for_session("session-b")
    assert [row.get("event") for row in writer_rows] == [None, "stale"]
    assert "scope" not in writer_rows[-1]


def test_unrelated_writes_do_not_mark_other_sessions():
    _evidence("session-a")

    mark_evidence_stale("unrelated.py", "session-b")

    assert "[stale]" not in (collect_evidence_summary(session_key="session-a") or "")


def test_disabling_the_switch_restores_per_session_behaviour(
    monkeypatch: pytest.MonkeyPatch,
):
    _evidence("session-a")
    monkeypatch.setitem(EVIDENCE_CROSS_SESSION_STALENESS, "enabled", False)

    mark_evidence_stale(PATH, "session-b")

    assert "[stale]" not in (collect_evidence_summary(session_key="session-a") or "")
    # The writer's own session is still marked.
    assert EvidenceLedger.read_for_session("session-b")[-1]["event"] == "stale"


def test_the_scan_cap_limits_how_many_sessions_are_marked(
    monkeypatch: pytest.MonkeyPatch,
):
    for index in range(5):
        _evidence(f"session-{index}")
    monkeypatch.setitem(EVIDENCE_CROSS_SESSION_STALENESS, "max_sessions_scan", 2)

    marked = EvidenceLedger.mark_stale_for_path_all_sessions(PATH, "session-writer")

    assert marked == 2


def test_command_matching_does_not_infer_source_dependencies():
    """Known boundary of §2: only commands that NAME the path are invalidated.

    A test command ``pytest tests/test_beta.py`` does not name the source
    module it imports, so a sibling writing that module is not detected here.
    Closing that gap needs the (deferred) re-verification gate.
    """
    _evidence("session-a", "pytest tests/test_beta.py")

    mark_evidence_stale("src/alpha.py", "session-b")

    assert "[stale]" not in (collect_evidence_summary(session_key="session-a") or "")


def test_a_ledger_failure_does_not_break_the_write_path(
    monkeypatch: pytest.MonkeyPatch,
):
    _evidence("session-a")

    def _boom(*_args, **_kwargs):
        raise RuntimeError("ledger unavailable")

    monkeypatch.setattr(EvidenceLedger, "mark_stale_for_path_all_sessions", _boom)

    mark_evidence_stale(PATH, "session-b")  # must not raise

    # The per-session mark committed before the broadcast failed.
    assert "[stale]" not in (collect_evidence_summary(session_key="session-a") or "")
    assert EvidenceLedger.read_for_session("session-b")[-1]["event"] == "stale"
