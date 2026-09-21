"""T3.1: terminal / python_repl auto-record verification evidence (fail-open).

Spawn points are monkeypatched so no real subprocess runs; the ledger is
redirected into ``tmp_path`` so the repo ledger is never touched.
"""

from __future__ import annotations

import pytest

from config.features import EVIDENCE_LEDGER
from agent.tools.pub_base.sandbox import SandboxPolicy
from agent.tools.todolist import evidence_recorder
from agent.tools.todolist.evidence_ledger import EvidenceLedger

import agent.tools.terminal as terminal
import agent.tools.python_repl as python_repl

pytestmark = [pytest.mark.unit]


class _FakeRunManager:
    def __init__(self, session_id: str = "s1"):
        self.config = {"configurable": {"session_id": session_id}}


@pytest.fixture
def ledger_path(tmp_path, monkeypatch):
    path = tmp_path / "evidence.jsonl"
    monkeypatch.setattr(EvidenceLedger, "LEDGER_PATH", str(path))
    return path


@pytest.fixture
def no_sandbox(monkeypatch):
    monkeypatch.setattr(terminal, "read_policy", lambda: SandboxPolicy.OFF)


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        ("pytest -q tests/x.py", "test"),
        ("ruff check .", "lint"),
        ("cargo build", "build"),
        ("basedpyright agent/", "typecheck"),
        ("ls -la", None),
    ],
)
def test_classify_verification_command(command, expected):
    assert evidence_recorder.classify_verification_command(command) == expected


def test_record_appends_passed_row(ledger_path):
    evidence_recorder.record_verification_evidence("pytest -q", "all good", "s1")

    row = EvidenceLedger.read_all()[0]
    assert row["kind"] == "test"
    assert row["status"] == "passed"
    assert row["exit_code"] == 0
    assert row["session_id"] == "s1"
    assert row["command"] == "pytest -q"


def test_record_marks_failed_from_exit_code(ledger_path):
    evidence_recorder.record_verification_evidence("pytest -q", "Exit code 2\nboom", "s1")

    row = EvidenceLedger.read_all()[0]
    assert row["exit_code"] == 2
    assert row["status"] == "failed"


def test_record_parses_repl_subprocess_exit_code(ledger_path):
    evidence_recorder.record_verification_evidence(
        "run pytest", "Error: subprocess exited with code 3\nboom", "s1"
    )

    row = EvidenceLedger.read_all()[0]
    assert row["exit_code"] == 3
    assert row["status"] == "failed"


def test_unclassified_command_is_not_recorded(ledger_path):
    evidence_recorder.record_verification_evidence("ls -la", "a b c", "s1")

    assert EvidenceLedger.read_all() == []


def test_auto_record_disabled_skips(ledger_path, monkeypatch):
    monkeypatch.setitem(EVIDENCE_LEDGER, "auto_record", False)

    evidence_recorder.record_verification_evidence("pytest -q", "ok", "s1")

    assert EvidenceLedger.read_all() == []


def test_terminal_records_passed_pytest_evidence(ledger_path, no_sandbox, monkeypatch):
    monkeypatch.setattr(
        terminal.SafeShellTool, "_run_with_encoding", lambda self, commands, encoding, env: "ok"
    )
    tool = terminal.build_terminal_tool()

    result = tool._run(["pytest", "-q"], run_manager=_FakeRunManager("s1"))

    assert result == "ok"
    row = EvidenceLedger.read_all()[0]
    assert row["kind"] == "test"
    assert row["status"] == "passed"


def test_terminal_records_failed_exit_code(ledger_path, no_sandbox, monkeypatch):
    canned = "Exit code 5\n3 failed"
    monkeypatch.setattr(
        terminal.SafeShellTool,
        "_run_with_encoding",
        lambda self, commands, encoding, env: canned,
    )
    tool = terminal.build_terminal_tool()

    assert tool._run(["pytest"], run_manager=_FakeRunManager("s1")) == canned
    assert EvidenceLedger.read_all()[0]["exit_code"] == 5


def test_terminal_non_verification_not_recorded(ledger_path, no_sandbox, monkeypatch):
    monkeypatch.setattr(
        terminal.SafeShellTool, "_run_with_encoding", lambda self, commands, encoding, env: "files"
    )
    tool = terminal.build_terminal_tool()

    tool._run(["ls"], run_manager=_FakeRunManager("s1"))

    assert EvidenceLedger.read_all() == []


def test_terminal_result_survives_ledger_write_failure(ledger_path, no_sandbox, monkeypatch):
    monkeypatch.setattr(
        terminal.SafeShellTool, "_run_with_encoding", lambda self, commands, encoding, env: "ok"
    )
    tool = terminal.build_terminal_tool()

    def _boom(entry):
        raise RuntimeError("disk full")

    monkeypatch.setattr(EvidenceLedger, "append", _boom)

    assert tool._run(["pytest"], run_manager=_FakeRunManager("s1")) == "ok"


def test_python_repl_records_test_evidence(ledger_path, monkeypatch):
    monkeypatch.setattr(python_repl, "_run_with_timeout", lambda command, timeout, sandbox: "ok")
    tool = python_repl.build_python_repl_tool()
    query = "import subprocess; subprocess.run(['pytest', '-q'])"

    assert tool._run(query, run_manager=_FakeRunManager("s2")) == "ok"
    row = EvidenceLedger.read_all()[0]
    assert row["kind"] == "test"
    assert row["session_id"] == "s2"


def test_python_repl_non_verification_not_recorded(ledger_path, monkeypatch):
    monkeypatch.setattr(python_repl, "_run_with_timeout", lambda command, timeout, sandbox: "2")
    tool = python_repl.build_python_repl_tool()

    tool._run("print(1 + 1)", run_manager=_FakeRunManager("s2"))

    assert EvidenceLedger.read_all() == []
