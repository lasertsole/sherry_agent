"""Unit tests for EvidenceLedger: append-only JSONL with UTC ISO timestamps.

The ledger is a class-level file API, so tests redirect ``LEDGER_PATH`` into
``tmp_path`` and never touch the real ``.omo/ledger.jsonl``.
"""

import json
from datetime import datetime, timedelta

import pytest

from agent.tools.todolist.evidence_ledger import EvidenceLedger

pytestmark = [pytest.mark.unit]


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
