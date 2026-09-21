"""Unit tests for EvidenceLedger: append-only JSONL with UTC ISO timestamps.

The ledger is a class-level file API, so tests redirect ``LEDGER_PATH`` into
``tmp_path`` and never touch the real repo ledger.
"""

import json
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from agent.tools.todolist.evidence_ledger import EvidenceLedger

pytestmark = [pytest.mark.unit]


def test_default_ledger_path_is_repo_absolute():
    assert Path(EvidenceLedger.LEDGER_PATH).is_absolute()


def test_append_read_all_roundtrip_and_appends_not_overwrites(
    tmp_path, monkeypatch: pytest.MonkeyPatch
):
    ledger = tmp_path / "ledger.jsonl"
    monkeypatch.setattr(EvidenceLedger, "LEDGER_PATH", str(ledger))

    EvidenceLedger.append({"event": "task-started", "task": "t1"})
    EvidenceLedger.append({"event": "task-completed", "task": "t1", "commands": ["pytest -q"]})

    rows = EvidenceLedger.read_all()
    assert len(rows) == 2
    assert rows[0]["event"] == "task-started"
    assert rows[1]["event"] == "task-completed"
    assert rows[1]["commands"] == ["pytest -q"]

    raw_lines = ledger.read_text(encoding="utf-8").splitlines()
    assert len(raw_lines) == 2
    for line in raw_lines:
        json.loads(line)  # every line is exactly one JSON object


def test_append_stamps_utc_iso_timestamp(tmp_path, monkeypatch: pytest.MonkeyPatch):
    ledger = tmp_path / "ledger.jsonl"
    monkeypatch.setattr(EvidenceLedger, "LEDGER_PATH", str(ledger))

    EvidenceLedger.append({"event": "task-started"})

    stamped = datetime.fromisoformat(EvidenceLedger.read_all()[0]["timestamp"])
    assert stamped.tzinfo is not None
    assert stamped.utcoffset() == timedelta(0)


def test_append_does_not_mutate_caller_entry(tmp_path, monkeypatch: pytest.MonkeyPatch):
    ledger = tmp_path / "ledger.jsonl"
    monkeypatch.setattr(EvidenceLedger, "LEDGER_PATH", str(ledger))
    entry = {"event": "task-started"}

    EvidenceLedger.append(entry)

    assert entry == {"event": "task-started"}


def test_read_all_missing_file_returns_empty(tmp_path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(EvidenceLedger, "LEDGER_PATH", str(tmp_path / "missing.jsonl"))

    assert EvidenceLedger.read_all() == []


def test_append_creates_missing_parent_directory(tmp_path, monkeypatch: pytest.MonkeyPatch):
    ledger = tmp_path / "nested" / "deeper" / "ledger.jsonl"
    monkeypatch.setattr(EvidenceLedger, "LEDGER_PATH", str(ledger))

    EvidenceLedger.append({"event": "task-started"})

    assert ledger.exists()
    assert len(EvidenceLedger.read_all()) == 1


# ── T3.0: session-scoped views + stale events ───────────────────────────────


def test_read_for_session_isolates_sessions(tmp_path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(EvidenceLedger, "LEDGER_PATH", str(tmp_path / "ledger.jsonl"))
    EvidenceLedger.for_session("s1").append(kind="test", command="pytest -q")
    EvidenceLedger.for_session("s2").append(kind="lint", command="ruff check .")

    assert [r["command"] for r in EvidenceLedger.read_for_session("s1")] == ["pytest -q"]
    assert [r["command"] for r in EvidenceLedger.read_for_session("s2")] == ["ruff check ."]


def test_session_ledger_append_injects_session_id(tmp_path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(EvidenceLedger, "LEDGER_PATH", str(tmp_path / "ledger.jsonl"))

    EvidenceLedger.for_session("s1").append(kind="test", command="pytest -q")

    assert EvidenceLedger.read_all()[0]["session_id"] == "s1"
    assert EvidenceLedger.for_session("s1").list_records()[0]["kind"] == "test"


def test_mark_stale_appends_event_without_rewriting_history(
    tmp_path, monkeypatch: pytest.MonkeyPatch
):
    ledger = tmp_path / "ledger.jsonl"
    monkeypatch.setattr(EvidenceLedger, "LEDGER_PATH", str(ledger))
    EvidenceLedger.for_session("s1").append(kind="test", command="pytest foo.py")
    before = ledger.read_text(encoding="utf-8").splitlines()

    EvidenceLedger.for_session("s1").mark_stale_for_path("foo.py")

    after = ledger.read_text(encoding="utf-8").splitlines()
    assert after[: len(before)] == before
    assert len(after) == len(before) + 1
    stale = EvidenceLedger.read_all()[-1]
    assert stale["event"] == "stale"
    assert stale["file_path"] == "foo.py"
    assert stale["session_id"] == "s1"


def test_mark_stale_returns_matching_count_scoped_to_session(
    tmp_path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(EvidenceLedger, "LEDGER_PATH", str(tmp_path / "ledger.jsonl"))
    EvidenceLedger.for_session("s1").append(kind="test", command="pytest foo.py")
    EvidenceLedger.for_session("s1").append(kind="lint", command="ruff check bar.py")
    EvidenceLedger.for_session("s2").append(kind="test", command="pytest foo.py")

    assert EvidenceLedger.for_session("s1").mark_stale_for_path("foo.py") == 1
    assert EvidenceLedger.mark_stale_for_path("foo.py") == 2


def test_mark_stale_ignores_prior_stale_events(tmp_path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(EvidenceLedger, "LEDGER_PATH", str(tmp_path / "ledger.jsonl"))
    EvidenceLedger.for_session("s1").append(kind="test", command="pytest foo.py")
    EvidenceLedger.for_session("s1").mark_stale_for_path("foo.py")

    assert EvidenceLedger.for_session("s1").mark_stale_for_path("foo.py") == 1
    assert len(EvidenceLedger.read_all()) == 3
