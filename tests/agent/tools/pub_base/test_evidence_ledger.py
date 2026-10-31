"""Unit tests for EvidenceLedger: SQLite rows with an unchanged public API.

The ledger is a class-level store, so tests redirect ``LEDGER_PATH`` into
``tmp_path`` and never touch the real repo ledger. SQLite-specific behavior
(WAL, pruning) has focused tests beside this file.
"""

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from agent.tools.pub_base.evidence_ledger import EvidenceLedger

pytestmark = [pytest.mark.unit]


def _redirect(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str = "ledger.db") -> Path:
    ledger = tmp_path / name
    monkeypatch.setattr(EvidenceLedger, "LEDGER_PATH", str(ledger))
    return ledger


def _rows(ledger: Path) -> list[tuple]:
    with sqlite3.connect(ledger) as conn:
        return conn.execute(
            "SELECT timestamp, session_id, event, file_path, command, status, kind "
            "FROM evidence_events ORDER BY event_id"
        ).fetchall()


def test_default_ledger_path_is_repo_absolute_sqlite():
    path = Path(EvidenceLedger.LEDGER_PATH)
    assert path.is_absolute()
    assert path.suffix == ".db"


def test_append_read_all_roundtrip_and_inserts_do_not_overwrite(
    tmp_path, monkeypatch: pytest.MonkeyPatch
):
    ledger = _redirect(tmp_path, monkeypatch)

    EvidenceLedger.append({"event": "task-started", "task": "t1"})
    EvidenceLedger.append({"event": "task-completed", "task": "t1", "commands": ["pytest -q"]})

    rows = EvidenceLedger.read_all()
    assert len(rows) == 2
    assert rows[0]["event"] == "task-started"
    assert rows[1]["event"] == "task-completed"
    assert rows[1]["commands"] == ["pytest -q"]
    assert len(_rows(ledger)) == 2


def test_unknown_fields_survive_the_payload_column(tmp_path, monkeypatch: pytest.MonkeyPatch):
    _redirect(tmp_path, monkeypatch)

    EvidenceLedger.for_session("s1").append(
        kind="test",
        command="pytest -q",
        status="passed",
        output_snippet="1 passed",
        sandboxed=False,
        future_field={"nested": [1, 2]},
    )

    row = EvidenceLedger.read_all()[0]
    assert row["output_snippet"] == "1 passed"
    assert row["sandboxed"] is False
    assert row["future_field"] == {"nested": [1, 2]}


def test_append_stamps_utc_iso_timestamp(tmp_path, monkeypatch: pytest.MonkeyPatch):
    _redirect(tmp_path, monkeypatch)

    EvidenceLedger.append({"event": "task-started"})

    stamped = datetime.fromisoformat(EvidenceLedger.read_all()[0]["timestamp"])
    assert stamped.tzinfo is not None
    assert stamped.utcoffset() == timedelta(0)


def test_append_does_not_mutate_caller_entry(tmp_path, monkeypatch: pytest.MonkeyPatch):
    _redirect(tmp_path, monkeypatch)
    entry = {"event": "task-started"}

    EvidenceLedger.append(entry)

    assert entry == {"event": "task-started"}


def test_read_all_missing_file_returns_empty(tmp_path, monkeypatch: pytest.MonkeyPatch):
    _redirect(tmp_path, monkeypatch, "missing.db")

    assert EvidenceLedger.read_all() == []


def test_append_creates_missing_parent_directory(tmp_path, monkeypatch: pytest.MonkeyPatch):
    ledger = tmp_path / "nested" / "deeper" / "ledger.db"
    monkeypatch.setattr(EvidenceLedger, "LEDGER_PATH", str(ledger))

    EvidenceLedger.append({"event": "task-started"})

    assert ledger.exists()
    assert len(EvidenceLedger.read_all()) == 1


# ── Session-scoped views + stale events ─────────────────────────────────────


def test_read_for_session_isolates_sessions(tmp_path, monkeypatch: pytest.MonkeyPatch):
    _redirect(tmp_path, monkeypatch)
    EvidenceLedger.for_session("s1").append(kind="test", command="pytest -q")
    EvidenceLedger.for_session("s2").append(kind="lint", command="ruff check .")

    assert [r["command"] for r in EvidenceLedger.read_for_session("s1")] == ["pytest -q"]
    assert [r["command"] for r in EvidenceLedger.read_for_session("s2")] == ["ruff check ."]


def test_read_for_flow_isolates_flows_and_excludes_unstamped_rows(
    tmp_path, monkeypatch: pytest.MonkeyPatch
):
    _redirect(tmp_path, monkeypatch)
    EvidenceLedger.for_session("s1").append(kind="test", command="pytest a.py", flow_id="f1")
    EvidenceLedger.for_session("s2").append(kind="lint", command="ruff b.py", flow_id="f2")
    EvidenceLedger.for_session("s3").append(kind="build", command="cargo build")

    assert [r["command"] for r in EvidenceLedger.read_for_flow("f1")] == ["pytest a.py"]
    assert [r["command"] for r in EvidenceLedger.read_for_flow("f2")] == ["ruff b.py"]
    assert EvidenceLedger.read_for_flow("") == []


def test_rows_stamp_the_session_flow_context(tmp_path, monkeypatch: pytest.MonkeyPatch):
    """Verification and stale rows both inherit the session's active flow id."""
    from runtime import StateKey, state_register_mem

    _redirect(tmp_path, monkeypatch)
    state_register_mem.set_state("s1", StateKey.FLOW_ID, "f1")
    try:
        EvidenceLedger.for_session("s1").append(kind="test", command="pytest a.py", status="passed")
        EvidenceLedger.for_session("s1").mark_stale_for_path("a.py")
    finally:
        state_register_mem.clear_session("s1")

    rows = EvidenceLedger.read_for_flow("f1")
    assert [row.get("event") for row in rows] == [None, "stale"]
    assert all(row["flow_id"] == "f1" for row in rows)


def test_session_ledger_append_injects_session_id(tmp_path, monkeypatch: pytest.MonkeyPatch):
    _redirect(tmp_path, monkeypatch)

    EvidenceLedger.for_session("s1").append(kind="test", command="pytest -q")

    assert EvidenceLedger.read_all()[0]["session_id"] == "s1"
    assert EvidenceLedger.for_session("s1").list_records()[0]["kind"] == "test"


def test_mark_stale_appends_event_without_rewriting_history(
    tmp_path, monkeypatch: pytest.MonkeyPatch
):
    _redirect(tmp_path, monkeypatch)
    EvidenceLedger.for_session("s1").append(kind="test", command="pytest foo.py")
    before = EvidenceLedger.read_all()

    EvidenceLedger.for_session("s1").mark_stale_for_path("foo.py")

    after = EvidenceLedger.read_all()
    assert after[: len(before)] == before
    assert len(after) == len(before) + 1
    stale = after[-1]
    assert stale["event"] == "stale"
    assert stale["file_path"] == "foo.py"
    assert stale["session_id"] == "s1"


def test_mark_stale_returns_matching_count_scoped_to_session(
    tmp_path, monkeypatch: pytest.MonkeyPatch
):
    _redirect(tmp_path, monkeypatch)
    EvidenceLedger.for_session("s1").append(kind="test", command="pytest foo.py")
    EvidenceLedger.for_session("s1").append(kind="lint", command="ruff check bar.py")
    EvidenceLedger.for_session("s2").append(kind="test", command="pytest foo.py")

    assert EvidenceLedger.for_session("s1").mark_stale_for_path("foo.py") == 1
    assert EvidenceLedger.mark_stale_for_path("foo.py") == 2


def test_mark_stale_ignores_prior_stale_events(tmp_path, monkeypatch: pytest.MonkeyPatch):
    _redirect(tmp_path, monkeypatch)
    EvidenceLedger.for_session("s1").append(kind="test", command="pytest foo.py")
    EvidenceLedger.for_session("s1").mark_stale_for_path("foo.py")

    assert EvidenceLedger.for_session("s1").mark_stale_for_path("foo.py") == 1
    assert len(EvidenceLedger.read_all()) == 3


# ── Concurrency / grouping ─────────────────────────────────────────────────


def test_concurrent_appends_are_not_lost(tmp_path, monkeypatch: pytest.MonkeyPatch):
    import threading

    _redirect(tmp_path, monkeypatch)
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


def test_read_for_keys_groups_in_insertion_order(tmp_path, monkeypatch: pytest.MonkeyPatch):
    _redirect(tmp_path, monkeypatch)
    EvidenceLedger.for_session("a").append(kind="test", command="pytest a1", status="passed")
    EvidenceLedger.for_session("b").append(kind="test", command="pytest b1", status="passed")
    EvidenceLedger.for_session("a").append(kind="test", command="pytest a2", status="passed")

    grouped = EvidenceLedger.read_for_keys(["a", "b", "ghost"])

    assert set(grouped) == {"a", "b"}
    assert [row["command"] for row in grouped["a"]] == ["pytest a1", "pytest a2"]
    assert [row["command"] for row in grouped["b"]] == ["pytest b1"]


def test_read_for_keys_with_no_keys_reads_nothing(tmp_path, monkeypatch: pytest.MonkeyPatch):
    _redirect(tmp_path, monkeypatch)
    EvidenceLedger.append({"kind": "test", "command": "pytest -q", "status": "passed"})

    assert EvidenceLedger.read_for_keys(["", "   "]) == {}


# ── Purge ──────────────────────────────────────────────────────────────────


def test_purge_session_deletes_only_that_session(tmp_path, monkeypatch: pytest.MonkeyPatch):
    _redirect(tmp_path, monkeypatch)
    EvidenceLedger.for_session("s1").append(kind="test", command="pytest -q")
    EvidenceLedger.for_session("s2").append(kind="test", command="pytest -q")

    removed = EvidenceLedger.purge_session("s1")

    assert removed == 1
    assert EvidenceLedger.read_for_session("s1") == []
    assert len(EvidenceLedger.read_for_session("s2")) == 1


def test_purge_orphans_keeps_known_and_unscoped_rows(tmp_path, monkeypatch: pytest.MonkeyPatch):
    _redirect(tmp_path, monkeypatch)
    EvidenceLedger.for_session("known").append(kind="test", command="pytest -q")
    EvidenceLedger.for_session("gone").append(kind="test", command="pytest -q")
    EvidenceLedger.mark_stale_for_path("foo.py")  # session_id=""

    removed = EvidenceLedger.purge_orphans({"known"})

    assert removed == 1
    assert [r["session_id"] for r in EvidenceLedger.read_all()] == ["known", ""]


# ── Pruning ────────────────────────────────────────────────────────────────


def _append_with_timestamp(record: dict, timestamp: datetime) -> None:
    """Insert a row with an explicit timestamp through the payload path."""
    from agent.tools.pub_base import evidence_ledger as module

    with module.EvidenceLedger._connection() as conn:
        module.EvidenceLedger._insert(conn, {**record, "timestamp": timestamp.isoformat()})
        conn.commit()


def test_age_ttl_prunes_old_rows(tmp_path, monkeypatch: pytest.MonkeyPatch):
    from config.features import EVIDENCE_LEDGER

    monkeypatch.setattr(EvidenceLedger, "LEDGER_PATH", str(tmp_path / "ledger.db"))
    monkeypatch.setitem(EVIDENCE_LEDGER, "max_age_days", 30)
    monkeypatch.setitem(EVIDENCE_LEDGER, "pruning_enabled", True)
    _append_with_timestamp(
        {"session_id": "s", "kind": "test"}, datetime.now(UTC) - timedelta(days=31)
    )
    _append_with_timestamp({"session_id": "s", "kind": "test"}, datetime.now(UTC))

    EvidenceLedger.append({"session_id": "s", "kind": "lint"})

    assert len(EvidenceLedger.read_all()) == 2


def test_per_session_cap_keeps_newest(tmp_path, monkeypatch: pytest.MonkeyPatch):
    from config.features import EVIDENCE_LEDGER

    monkeypatch.setattr(EvidenceLedger, "LEDGER_PATH", str(tmp_path / "ledger.db"))
    monkeypatch.setitem(EVIDENCE_LEDGER, "pruning_enabled", True)
    monkeypatch.setitem(EVIDENCE_LEDGER, "max_age_days", 0)
    monkeypatch.setitem(EVIDENCE_LEDGER, "max_events_per_session", 3)
    monkeypatch.setitem(EVIDENCE_LEDGER, "max_total_events", 0)
    for n in range(5):
        EvidenceLedger.append({"session_id": "s", "kind": "test", "n": n})

    assert [row["n"] for row in EvidenceLedger.read_all()] == [2, 3, 4]


def test_global_cap_keeps_newest(tmp_path, monkeypatch: pytest.MonkeyPatch):
    from config.features import EVIDENCE_LEDGER

    monkeypatch.setattr(EvidenceLedger, "LEDGER_PATH", str(tmp_path / "ledger.db"))
    monkeypatch.setitem(EVIDENCE_LEDGER, "pruning_enabled", True)
    monkeypatch.setitem(EVIDENCE_LEDGER, "max_age_days", 0)
    monkeypatch.setitem(EVIDENCE_LEDGER, "max_events_per_session", 0)
    monkeypatch.setitem(EVIDENCE_LEDGER, "max_total_events", 4)
    for n in range(6):
        EvidenceLedger.append({"session_id": "s", "kind": "test", "n": n})

    assert [row["n"] for row in EvidenceLedger.read_all()] == [2, 3, 4, 5]


def test_pruning_disabled_keeps_everything(tmp_path, monkeypatch: pytest.MonkeyPatch):
    from config.features import EVIDENCE_LEDGER

    monkeypatch.setattr(EvidenceLedger, "LEDGER_PATH", str(tmp_path / "ledger.db"))
    monkeypatch.setitem(EVIDENCE_LEDGER, "pruning_enabled", False)
    monkeypatch.setitem(EVIDENCE_LEDGER, "max_events_per_session", 1)
    for n in range(4):
        EvidenceLedger.append({"session_id": "s", "kind": "test", "n": n})

    assert len(EvidenceLedger.read_all()) == 4
