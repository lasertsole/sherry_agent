"""File edits append evidence stale events (append-only, fail-open)."""

from __future__ import annotations

import pytest

from agent.tools.todolist.evidence_ledger import EvidenceLedger

import agent.tools.file_tools.patch_file as patch_file
import agent.tools.file_tools.write_file as write_file

pytestmark = [pytest.mark.unit]


@pytest.fixture
def ledger_path(tmp_path, monkeypatch):
    path = tmp_path / "evidence.jsonl"
    monkeypatch.setattr(EvidenceLedger, "LEDGER_PATH", str(path))
    return path


def test_write_file_appends_stale_event(ledger_path, tmp_path, monkeypatch):
    target = tmp_path / "foo.py"
    monkeypatch.setattr(write_file, "resolve_project_path", lambda _path: target)
    EvidenceLedger.for_session("s1").append(kind="test", command="pytest foo.py")
    tool = write_file.build_write_file_tool()

    result = tool._run("foo.py", "print('x')\n", session_id="s1")

    assert "successfully" in result
    stale = EvidenceLedger.read_all()[-1]
    assert stale["event"] == "stale"
    assert stale["file_path"] == "foo.py"
    assert stale["session_id"] == "s1"


def test_write_file_stale_event_derives_prior_evidence(ledger_path, tmp_path, monkeypatch):
    target = tmp_path / "foo.py"
    monkeypatch.setattr(write_file, "resolve_project_path", lambda _path: target)
    EvidenceLedger.for_session("s1").append(kind="test", command="pytest foo.py")
    tool = write_file.build_write_file_tool()

    tool._run("foo.py", "print('x')\n", session_id="s1")

    rows = EvidenceLedger.read_all()
    evidence, stale = rows[0], rows[1]
    assert stale["event"] == "stale"
    assert stale["file_path"] in evidence["command"]


def test_patch_file_appends_stale_event(ledger_path, tmp_path, monkeypatch):
    target = tmp_path / "bar.py"
    target.write_text("value = 1\n", encoding="utf-8")
    monkeypatch.setattr(patch_file, "resolve_project_path", lambda _path: target)
    EvidenceLedger.for_session("s1").append(kind="test", command="pytest bar.py")
    tool = patch_file.build_patch_file_tool()

    result = tool._run("bar.py", "value = 1", "value = 2", session_id="s1")

    assert '"success": true' in result
    stale = EvidenceLedger.read_all()[-1]
    assert stale["event"] == "stale"
    assert stale["file_path"] == "bar.py"
    assert stale["session_id"] == "s1"


def test_write_file_survives_ledger_failure(ledger_path, tmp_path, monkeypatch):
    target = tmp_path / "foo.py"
    monkeypatch.setattr(write_file, "resolve_project_path", lambda _path: target)
    tool = write_file.build_write_file_tool()

    def _boom(_entry):
        raise RuntimeError("disk full")

    monkeypatch.setattr(EvidenceLedger, "append", _boom)

    assert "successfully" in tool._run("foo.py", "print('x')\n", session_id="s1")
