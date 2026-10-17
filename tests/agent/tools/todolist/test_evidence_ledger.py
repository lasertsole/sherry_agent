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


# ── Session-scoped views + stale events ─────────────────────────────────────


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


class TestLedgerRobustness:
    """The write lock and the tolerant read: concurrent appends stay whole."""

    def _redirect(self, tmp_path, monkeypatch) -> Path:
        ledger = tmp_path / "robust.jsonl"
        monkeypatch.setattr(EvidenceLedger, "LEDGER_PATH", str(ledger))
        return ledger

    def test_append_takes_the_shared_write_lock(self, tmp_path, monkeypatch):
        """The lock is wired in: two processes must not interleave a line."""
        from agent.tools.todolist import evidence_ledger as module

        held: list[Path] = []

        class _SpyLock:
            def __init__(self, target: Path):
                held.append(Path(target))

            def __enter__(self):
                return None

            def __exit__(self, *_exc) -> bool:
                return False

        monkeypatch.setattr(module, "file_write_lock", _SpyLock)
        ledger = self._redirect(tmp_path, monkeypatch)

        EvidenceLedger.append({"kind": "test", "command": "pytest -q", "status": "passed"})

        assert held == [ledger]

    def test_a_broken_line_is_skipped_with_a_warning(self, tmp_path, monkeypatch):
        from loguru import logger as loguru_logger

        ledger = self._redirect(tmp_path, monkeypatch)
        ledger.write_text(
            '{"kind": "test", "command": "pytest -q", "status": "passed"}\n'
            '{"kind": "lint", "comma\n'  # torn by an old, unlocked writer
            '{"kind": "build", "command": "make", "status": "passed"}\n',
            encoding="utf-8",
        )

        records: list[str] = []
        sink_id = loguru_logger.add(
            lambda message: records.append(message.record["message"]), level="WARNING"
        )
        try:
            rows = EvidenceLedger.read_all()
        finally:
            loguru_logger.remove(sink_id)

        assert [row["kind"] for row in rows] == ["test", "build"]
        assert any("unparsable line" in line for line in records)

    def test_concurrent_appends_never_tear_a_line(self, tmp_path, monkeypatch):
        """Two threads appending at once: every line parses, none is lost."""
        import threading

        self._redirect(tmp_path, monkeypatch)
        writers = 2
        per_writer = 25
        barrier = threading.Barrier(writers)

        def _write(index: int) -> None:
            barrier.wait()
            for n in range(per_writer):
                EvidenceLedger.append({"kind": "test", "writer": index, "n": n})

        threads = [threading.Thread(target=_write, args=(i,)) for i in range(writers)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        rows = EvidenceLedger.read_all()

        assert len(rows) == writers * per_writer
        assert len({(row["writer"], row["n"]) for row in rows}) == writers * per_writer

    def test_read_for_keys_groups_in_file_order(self, tmp_path, monkeypatch):
        self._redirect(tmp_path, monkeypatch)
        EvidenceLedger.for_session("a").append(kind="test", command="pytest a1", status="passed")
        EvidenceLedger.for_session("b").append(kind="test", command="pytest b1", status="passed")
        EvidenceLedger.for_session("a").append(kind="test", command="pytest a2", status="passed")

        grouped = EvidenceLedger.read_for_keys(["a", "b", "ghost"])

        assert set(grouped) == {"a", "b"}
        assert [row["command"] for row in grouped["a"]] == ["pytest a1", "pytest a2"]
        assert [row["command"] for row in grouped["b"]] == ["pytest b1"]

    def test_read_for_keys_with_no_keys_reads_nothing(self, tmp_path, monkeypatch):
        self._redirect(tmp_path, monkeypatch)
        EvidenceLedger.append({"kind": "test", "command": "pytest -q", "status": "passed"})

        assert EvidenceLedger.read_for_keys(["", "   "]) == {}
