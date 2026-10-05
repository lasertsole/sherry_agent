"""Stage 3 acceptance: retention that never expires a live holder's snapshot.

The load-bearing assertion is R13's: a row older than the retention window is
KEPT while the process that captured it is alive, and only becomes deletable
once that process is provably gone. Age alone is a candidate list, never a
verdict — a long turn must be able to revert its own first write at hour six.
"""

import os
import sqlite3
import time

import pytest

from agent.tools.file_tools import snapshot as snapshot_mod
from agent.tools.file_tools.snapshot import rows_for_session, snapshot_store
from agent.tools.file_tools.snapshot_gc import (
    process_start_token,
    run_snapshot_gc,
    snapshot_owner_state,
    start_file_snapshot_gc,
)

pytestmark = [pytest.mark.unit, pytest.mark.timeout(120)]

SESSION = "s-snapshot-gc"


def _insert_row(
    *,
    path: str,
    captured_at: float,
    blob_sha256: str | None = None,
    before_size: int = 10,
    owner_pid: int = 0,
    owner_start: str = "",
    session_id: str = SESSION,
) -> int:
    snapshot_store.insert_row(
        session_id=session_id,
        path=path,
        root="/tmp",
        blob_sha256=blob_sha256,
        before_size=before_size,
        before_revision="mtime:1:size:10",
        after_revision="mtime:2:size:10",
        after_sha256="b" * 64,
        existed_before=True,
        tool_call_id="call-gc",
        captured_at=captured_at,
        owner_pid=owner_pid,
        owner_start=owner_start,
    )
    return rows_for_session(session_id)[-1].id


def _blob(session_id: str, sha256: str) -> "os.PathLike[str]":
    from agent.tools.file_tools.snapshot import _blob_path

    blob = _blob_path(session_id, sha256)
    assert blob is not None
    blob.parent.mkdir(parents=True, exist_ok=True)
    blob.write_bytes(b"old content")
    return blob


@pytest.fixture
def sessions_dir(tmp_path, monkeypatch):
    """A SESSIONS_DIR carrying only the session this test writes rows for."""
    from config import path as path_mod

    root = tmp_path / "sessions"
    (root / SESSION).mkdir(parents=True)
    monkeypatch.setattr(path_mod, "SESSIONS_DIR", root)
    yield root


def test_an_aged_row_of_a_live_process_is_kept(sessions_dir):
    """R13: 8 hours old, holder alive — untouched, however far past retention."""
    pid = os.getpid()
    token = process_start_token(pid)
    assert token  # the platform can identify this process, or the test is moot
    _insert_row(
        path="/tmp/live.txt",
        captured_at=time.time() - 20 * 86400,
        owner_pid=pid,
        owner_start=token,
    )

    result = run_snapshot_gc()

    assert rows_for_session(SESSION) != []
    assert result.rows_skipped_live >= 1


def test_the_same_row_goes_once_its_holder_is_dead(sessions_dir):
    """A pid nobody owns (ESRCH) is the proven-dead case — no start comparison."""
    dead_pid = 999_999  # nothing runs here
    _insert_row(
        path="/tmp/dead.txt",
        captured_at=time.time() - 20 * 86400,
        owner_pid=dead_pid,
        owner_start="proc:1",
    )

    run_snapshot_gc()

    assert rows_for_session(SESSION) == []


def test_a_recycled_pid_is_dead(sessions_dir):
    """kill(pid, 0) succeeds but the start token differs: the original is gone."""
    pid = os.getpid()
    _insert_row(
        path="/tmp/recycled.txt",
        captured_at=time.time() - 20 * 86400,
        owner_pid=pid,
        owner_start="proc:0000000000000001",  # not this process's token
    )

    run_snapshot_gc()

    assert rows_for_session(SESSION) == []


def test_an_unknown_holder_gets_the_longer_grace_period(sessions_dir):
    _insert_row(
        path="/tmp/unknown.txt",
        captured_at=time.time() - 20 * 86400,  # past retention, inside the 30-day grace
        owner_pid=0,
        owner_start="",
    )

    run_snapshot_gc()

    assert len(rows_for_session(SESSION)) == 1


def test_an_unknown_holder_older_than_the_grace_is_collected(sessions_dir, monkeypatch):
    from config.features import FILE_SNAPSHOT

    monkeypatch.setitem(FILE_SNAPSHOT, "orphan_grace_days", 1)
    _insert_row(
        path="/tmp/ancient.txt",
        captured_at=time.time() - 20 * 86400,  # past retention AND past the grace
        owner_pid=0,
        owner_start="",
    )

    run_snapshot_gc()

    assert rows_for_session(SESSION) == []


def test_fresh_rows_are_never_touched(sessions_dir):
    _insert_row(path="/tmp/fresh.txt", captured_at=time.time(), owner_pid=999_999)

    run_snapshot_gc()

    assert len(rows_for_session(SESSION)) == 1


def test_the_file_cap_drops_the_oldest_first(sessions_dir, monkeypatch):
    from config.features import FILE_SNAPSHOT

    monkeypatch.setitem(FILE_SNAPSHOT, "max_files_per_session", 2)
    now = time.time()
    for index in range(4):
        _insert_row(
            path=f"/tmp/cap-{index}.txt",
            captured_at=now - (10 - index),  # strictly increasing
            owner_pid=999_999,
            owner_start="proc:1",
        )

    run_snapshot_gc()

    remaining = [row.path for row in rows_for_session(SESSION)]
    assert remaining == ["/tmp/cap-2.txt", "/tmp/cap-3.txt"]


def test_the_byte_cap_drops_the_oldest_first(sessions_dir, monkeypatch):
    from config.features import FILE_SNAPSHOT

    monkeypatch.setitem(FILE_SNAPSHOT, "max_bytes_per_session", 30)
    now = time.time()
    for index in range(3):
        _insert_row(
            path=f"/tmp/byte-{index}.txt",
            captured_at=now - (10 - index),
            before_size=20,
            owner_pid=999_999,
            owner_start="proc:1",
        )

    run_snapshot_gc()

    remaining = rows_for_session(SESSION)
    assert [row.path for row in remaining] == ["/tmp/byte-2.txt"]


def test_a_collected_row_takes_its_unreferenced_blob_with_it(sessions_dir):
    sha = "a" * 64
    blob = _blob(SESSION, sha)
    _insert_row(
        path="/tmp/blob.txt",
        captured_at=time.time() - 20 * 86400,
        blob_sha256=sha,
        owner_pid=999_999,
        owner_start="proc:1",
    )

    result = run_snapshot_gc()

    assert not blob.exists()
    assert result.blobs_deleted >= 1


def test_a_blob_another_row_still_references_survives(sessions_dir):
    sha = "c" * 64
    blob = _blob(SESSION, sha)
    _insert_row(
        path="/tmp/shared-old.txt",
        captured_at=time.time() - 21 * 86400,
        blob_sha256=sha,
        owner_pid=999_999,
        owner_start="proc:1",
    )
    _insert_row(
        path="/tmp/shared-fresh.txt",
        captured_at=time.time(),
        blob_sha256=sha,
        owner_pid=999_999,
        owner_start="proc:1",
    )

    run_snapshot_gc()

    assert blob.exists()
    assert [row.path for row in rows_for_session(SESSION)] == ["/tmp/shared-fresh.txt"]


def test_an_orphan_blob_is_swept_after_the_grace(sessions_dir, monkeypatch):
    from config.features import FILE_SNAPSHOT

    monkeypatch.setitem(FILE_SNAPSHOT, "orphan_grace_days", 0)
    orphan = _blob(SESSION, "d" * 64)
    old = time.time() - 3600
    os.utime(orphan, (old, old))

    run_snapshot_gc()

    assert not orphan.exists()


def test_the_batch_limit_bounds_one_sweep(sessions_dir, monkeypatch):
    from config.features import FILE_SNAPSHOT

    monkeypatch.setitem(FILE_SNAPSHOT, "gc_batch_limit", 2)
    now = time.time()
    for index in range(5):
        _insert_row(
            path=f"/tmp/batch-{index}.txt",
            captured_at=now - 20 * 86400,  # aged, and held by a provably dead pid
            owner_pid=999_999,
            owner_start="proc:1",
        )

    result = run_snapshot_gc()

    assert result.rows_deleted == 2
    assert len(rows_for_session(SESSION)) == 3


def test_a_gc_tick_never_raises(sessions_dir, monkeypatch):
    """A broken store must not kill the daemon thread."""
    from agent.tools.file_tools import snapshot_gc as gc_mod

    def boom(*_args, **_kwargs):
        raise sqlite3.OperationalError("database is locked")

    _insert_row(path="/tmp/any.txt", captured_at=time.time() - 20 * 86400)
    monkeypatch.setattr(gc_mod.snapshot_store, "rows_for_session", boom)

    # The sweep swallows the read failure per session and returns a result.
    result = run_snapshot_gc()

    assert result.rows_deleted == 0


def test_the_daemon_thread_is_idempotent(monkeypatch):
    """The started thread must not touch the store from the test process:
    a live sweeper racing temp directories kept wedging the suite."""
    from config.features import FILE_SNAPSHOT

    from agent.tools.file_tools import snapshot_gc as gc_mod

    monkeypatch.setitem(FILE_SNAPSHOT, "gc_interval_seconds", 3600)
    monkeypatch.setattr(gc_mod, "run_snapshot_gc", lambda **_: None)  # no store I/O

    started = start_file_snapshot_gc()
    again = start_file_snapshot_gc()

    assert started is not None and started.daemon is True
    assert again is started


def test_owner_state_reads_the_local_process(sessions_dir):
    from agent.tools.file_tools.snapshot import SnapshotRow

    token = process_start_token(os.getpid())
    row = SnapshotRow(
        id=1,
        session_id=SESSION,
        path="/tmp/x",
        root="/tmp",
        blob_sha256=None,
        before_size=0,
        before_revision="",
        after_revision="",
        after_sha256="",
        existed_before=False,
        tool_call_id="",
        captured_at=time.time(),
        owner_pid=os.getpid(),
        owner_start=token or "",
    )

    assert snapshot_owner_state(row) == "alive"
    assert (
        snapshot_owner_state(
            row.__class__(
                id=2,
                session_id=SESSION,
                path="/tmp/x",
                root="/tmp",
                blob_sha256=None,
                before_size=0,
                before_revision="",
                after_revision="",
                after_sha256="",
                existed_before=False,
                tool_call_id="",
                captured_at=time.time(),
                owner_pid=999_999,
                owner_start="proc:1",
            )
        )
        == "dead"
    )
    assert snapshot_mod.rows_for_session(SESSION) == []
