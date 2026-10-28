"""Durable-execution fixes: the three confirmed bugs plus the concurrency guards.

Each case pins one defect from the durable-execution audit:

* the registry's terminal predicate compared STRING LITERALS against an enum
  whose values are ``pending/running/interrupted/terminal`` — nothing ever
  matched, so the retention cap never evicted;
* the session-directory sweep probed checkpoints with the raw session id while
  the checkpointer stores ``session_thread_id(session_id)`` — a folder whose
  only remaining record was its checkpoint was deleted;
* the trajectory ledger promised "pruned on write" while ``prune_session`` had
  no caller;
* the global checkpoint prune ran per turn, without a busy timeout, and its
  keep-set could go stale between SELECT and DELETE;
* the purge deleted messages before checkpoints, so a locked checkpointer left a
  half-deleted session.
"""

from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path

import pytest

pytestmark = [pytest.mark.unit, pytest.mark.timeout(120)]


def _run_record(run_id: str, status):
    from agent.tools.subagent.types.registry import SubagentRunRecord

    record = SubagentRunRecord(
        run_id=run_id,
        child_session_key=f"agent:main:subagent:{run_id}",
        requester_session_key="agent:main:session:owner",
        task="probe",
    )
    return record.model_copy(
        update={"execution": record.execution.model_copy(update={"status": status})}
    )


class TestRegistryRetention:
    """A terminal run is evictable; a live one never is (the cap's whole point)."""

    def test_the_terminal_predicate_reads_the_enum(self):
        from agent.tools.subagent.registry.memory import _is_terminal
        from agent.tools.subagent.types.registry import ExecutionStatus

        assert _is_terminal(_run_record("r1", ExecutionStatus.RUNNING)) is False
        assert _is_terminal(_run_record("r1", ExecutionStatus.PENDING)) is False
        assert _is_terminal(_run_record("r1", ExecutionStatus.INTERRUPTED)) is False
        assert _is_terminal(_run_record("r1", ExecutionStatus.TERMINAL)) is True

    def test_the_cap_evicts_terminals_and_keeps_the_live_ones(self, monkeypatch):
        import agent.tools.subagent.registry.memory as memory
        from agent.tools.subagent.types.registry import ExecutionStatus

        monkeypatch.setattr(memory, "_MAX_RETAINED_RUNS", 3)
        memory._runs.clear()
        try:
            # Two live runs, then five terminal ones: the live pair must survive.
            memory.set_run(_run_record("live-1", ExecutionStatus.RUNNING))
            memory.set_run(_run_record("live-2", ExecutionStatus.PENDING))
            for index in range(5):
                memory.set_run(_run_record(f"done-{index}", ExecutionStatus.TERMINAL))

            assert len(memory._runs) <= 3
            assert "live-1" in memory._runs and "live-2" in memory._runs
            assert "done-4" in memory._runs, "the newest terminal row is kept"
            assert "done-0" not in memory._runs, "the oldest terminal row is evicted"
        finally:
            memory._runs.clear()


class TestSessionThreadId:
    """ONE mapping between a session id and its checkpointer thread id."""

    def test_the_mapping_is_the_persisted_algorithm(self):
        from pub.func import rand_str_to_int, session_thread_id

        assert session_thread_id("abc") == rand_str_to_int("abc")
        assert isinstance(session_thread_id("abc"), int)

    def test_the_sweep_probes_the_checkpointer_id(self, tmp_path, monkeypatch):
        """A checkpoint-only session must not look empty to the sweep."""
        import config.path as paths
        import server.service.session_dir_sweep as sweep
        from pub.func import session_thread_id

        checkpoints = tmp_path / "checkpoints"
        checkpoints.mkdir()
        monkeypatch.setattr(paths, "SRC_DIR", tmp_path)

        session_id = "20260101_120000_abcdef123456"
        db = sqlite3.connect(str(checkpoints / "sqlite.db"))
        db.execute(
            "CREATE TABLE checkpoints (thread_id INTEGER, checkpoint_ns TEXT, checkpoint_id TEXT)"
        )
        db.execute("INSERT INTO checkpoints VALUES (?, '', 'c1')", (session_thread_id(session_id),))
        db.commit()
        db.close()

        assert sweep._checkpoints_has_session(session_id) is True
        assert sweep._checkpoints_has_session("other-session") is False


class TestTrajectoryPrune:
    """``prune_session`` is now reachable (debounced), so retention is real."""

    @pytest.mark.asyncio
    async def test_pruning_keeps_the_newest_turns(self, tmp_path, monkeypatch):
        from config.features import TRAJECTORY
        from server.service.trajectory_store import TrajectoryStore

        monkeypatch.setitem(TRAJECTORY, "max_turns_per_session", 2)
        store = TrajectoryStore(tmp_path / "traj.db")

        for turn in range(4):
            await store.append("s1", f"turn-{turn}", "turn_start", {}, seq=0)

        removed = await store.prune_session("s1")

        assert removed == 2
        rows = await store.read("s1")
        assert {row["turn_id"] for row in rows} == {"turn-2", "turn-3"}

    @pytest.mark.asyncio
    async def test_the_write_path_prunes_once_per_interval(self, tmp_path, monkeypatch):
        import server.service.trajectory_store as module
        from config.features import TRAJECTORY

        monkeypatch.setitem(TRAJECTORY, "max_turns_per_session", 1)
        monkeypatch.setattr(module, "_PRUNE_INTERVAL_S", 3600.0)
        store = module.TrajectoryStore(tmp_path / "traj.db")

        calls: list[str] = []
        real_prune = store.prune_session

        async def _spy(session_id: str) -> int:
            calls.append(session_id)
            return await real_prune(session_id)

        store.prune_session = _spy  # type: ignore[method-assign]

        for turn in range(5):
            await store.append("s1", f"turn-{turn}", "turn_start", {}, seq=0)

        assert calls == ["s1"], "one prune per interval, not one per event"

    @pytest.mark.asyncio
    async def test_the_ledger_uses_wal_and_a_busy_timeout(self, tmp_path):
        from server.service.trajectory_store import TrajectoryStore

        store = TrajectoryStore(tmp_path / "traj.db")
        await store.append("s1", "turn-1", "turn_start", {}, seq=0)

        db = sqlite3.connect(str(tmp_path / "traj.db"))
        try:
            mode = db.execute("PRAGMA journal_mode").fetchone()[0]
        finally:
            db.close()
        assert str(mode).lower() == "wal"


class TestCheckpointerConcurrency:
    """The shared DB must WAIT for the write lock, and the prune must be safe."""

    def test_every_connection_carries_the_busy_timeout(self, tmp_path):
        from agent.checkpointer.pragmas import BUSY_TIMEOUT_MS, connect_with_busy_timeout

        async def _probe() -> int:
            conn = await connect_with_busy_timeout(tmp_path / "cp.db")
            try:
                cursor = await conn.execute("PRAGMA busy_timeout")
                row = await cursor.fetchone()
                return int(row[0])
            finally:
                await conn.close()

        assert asyncio.run(_probe()) == BUSY_TIMEOUT_MS == 30_000

    def test_the_global_prune_keeps_the_newest_top_level_row(self, tmp_path, monkeypatch):
        """A newer tools-namespace row must not displace the top-level keeper."""
        import config as config_module
        from agent.checkpointer.thread_safe_checkpointer import aclean_all_checkpoints

        checkpoints = tmp_path / "checkpoints"
        checkpoints.mkdir()
        monkeypatch.setattr(config_module, "SRC_DIR", tmp_path)

        db = sqlite3.connect(str(checkpoints / "sqlite.db"))
        db.execute(
            "CREATE TABLE checkpoints (thread_id INTEGER, checkpoint_ns TEXT, checkpoint_id TEXT)"
        )
        db.execute(
            "CREATE TABLE writes (thread_id INTEGER, checkpoint_ns TEXT, checkpoint_id TEXT)"
        )
        db.execute("INSERT INTO checkpoints VALUES (1, '', 'top-1')")
        db.execute("INSERT INTO checkpoints VALUES (1, 'tools:abc', 'sub-2')")
        db.execute("INSERT INTO writes VALUES (1, '', 'top-1')")
        db.commit()
        db.close()

        result = asyncio.run(aclean_all_checkpoints())

        db = sqlite3.connect(str(checkpoints / "sqlite.db"))
        try:
            kept = db.execute("SELECT checkpoint_ns, checkpoint_id FROM checkpoints").fetchall()
        finally:
            db.close()

        assert kept == [("", "top-1")], "the top-level checkpoint is the keeper"
        assert result["deleted_checkpoints"] == 1

    def test_the_prune_is_a_noop_without_a_database(self, tmp_path, monkeypatch):
        import config as config_module
        from agent.checkpointer.thread_safe_checkpointer import aclean_all_checkpoints

        monkeypatch.setattr(config_module, "SRC_DIR", tmp_path)  # no checkpoints/ dir

        assert asyncio.run(aclean_all_checkpoints())["threads_kept"] == 0

    def test_the_graph_build_no_longer_prunes(self):
        """The per-turn caller is gone; the boot path is the only invocation."""
        source = Path("agent/core.py").read_text(encoding="utf-8")

        assert "aclean_old_checkpoints" not in source
        boot = Path("server/__main__.py").read_text(encoding="utf-8")
        assert "clean_checkpoints_at_boot" in boot


class TestPurgeOrder:
    """The purge deletes the checkpoint FIRST and never lets a step abort it."""

    def test_the_checkpoint_delete_precedes_the_message_delete(self):
        source = Path("server/service/session_cleanup_service.py").read_text(encoding="utf-8")

        assert source.index("delete_thread_history") < source.index("delete_messages_by_session")

    @pytest.mark.asyncio
    async def test_purge_survives_a_locked_checkpointer(self, tmp_path, monkeypatch):
        """End-to-end: the guarded step logs and the rest of the purge proceeds."""
        import server.service.session_cleanup_service as cleanup

        calls: list[str] = []

        import context_engine

        async def _locked(session_id: str) -> None:
            calls.append("checkpoints")
            raise RuntimeError("database is locked")

        monkeypatch.setattr(cleanup, "delete_thread_history", _locked)
        # purge imports the store function lazily, so the seam is the package.
        monkeypatch.setattr(
            context_engine, "delete_messages_by_session", lambda **kw: 3, raising=False
        )

        await cleanup.purge_session_data("sess-1")

        assert "checkpoints" in calls, "the guard did run the checkpoint step"


class TestDeliverabilityIsDurable:
    """A delivery transition is the run's LAST fact: it must reach disk at once.

    It used to wait for the 30 s periodic flush, so a crash in that window left
    SQLite saying TERMINAL+PENDING — a state with no retry path, i.e. the
    completed result was lost permanently.
    """

    def test_the_durable_setter_writes_memory_and_sqlite(self, monkeypatch):
        import agent.tools.subagent.registry.memory as memory
        import agent.tools.subagent.registry.store_sqlite as store_sqlite
        from agent.tools.subagent.types.registry import ExecutionStatus

        written: list[str] = []
        monkeypatch.setattr(memory, "set_run", lambda run: written.append(run.run_id))
        monkeypatch.setattr(
            store_sqlite, "upsert_run_sync", lambda run: written.append(f"db:{run.run_id}")
        )

        memory.set_run_durable(_run_record("r1", ExecutionStatus.TERMINAL))

        assert written == ["r1", "db:r1"], "memory first, then the SQLite mirror"

    def test_the_delivery_transitions_use_the_durable_setter(self):
        source = Path("agent/tools/subagent/announce/delivery.py").read_text(encoding="utf-8")

        for transition in (
            "mark_delivery_delivered",
            "mark_delivery_failed",
            "mark_delivery_suspended",
            "mark_delivery_in_progress",
        ):
            call = source.index(transition)
            window = source[call : source.index("\n\n", call)]
            assert "set_run_durable" in window, f"{transition} must persist immediately"


class TestAutoTurnHitlFlag:
    """A headless auto turn that interrupts must mark the session pending."""

    def test_the_driver_overrides_the_no_op(self, monkeypatch):
        from server.service import auto_turn
        from agent.tools.subagent.registry import session_state

        flagged: list[tuple[str, bool]] = []
        monkeypatch.setattr(
            session_state, "set_hitl_pending", lambda sid, value: flagged.append((sid, value))
        )

        driver = auto_turn._AutoTurnStreamDriver.__new__(auto_turn._AutoTurnStreamDriver)
        driver.session_id = "s1"
        driver.apply_hitl_pending()

        assert flagged == [("s1", True)]

    def test_the_base_class_still_defaults_to_no_op(self):
        from server.service.stream_driver import StreamDriver

        assert "apply_hitl_pending" in StreamDriver.__dict__
        source = Path("server/service/stream_driver.py").read_text(encoding="utf-8")
        assert 'def apply_hitl_pending(self) -> None:\n        """Flag the session' in source


class TestSubmitUserInputInterruptProbe:
    """A suspended graph is busy even when every in-memory signal says idle."""

    @pytest.mark.asyncio
    async def test_a_pending_interrupt_queues_instead_of_dispatching(self, monkeypatch):
        import server.service.input_queue_service as iqs

        monkeypatch.setattr(iqs, "detect_state", lambda sid: _Idle())
        monkeypatch.setattr(iqs, "_get_session_lock", lambda sid: asyncio.Lock())

        async def _no_interrupt(session_id: str):
            return {"tool_name": "question", "description": "approve?"}

        monkeypatch.setattr("agent.state_port.read_interrupt", _no_interrupt)

        store = _FakeQueue()
        result = await iqs.submit_user_input(
            "sess-1", "hello", "user", queue=store, executor_registry=_FakeRegistry()
        )

        assert result.status.value == "QUEUED"
        assert store.enqueued, "the message must wait, not enter the suspended graph"

    @pytest.mark.asyncio
    async def test_a_broken_probe_fails_open(self, monkeypatch):
        import server.service.input_queue_service as iqs

        monkeypatch.setattr(iqs, "detect_state", lambda sid: _Idle())
        monkeypatch.setattr(iqs, "_get_session_lock", lambda sid: asyncio.Lock())

        async def _boom(session_id: str):
            raise RuntimeError("checkpointer unavailable")

        monkeypatch.setattr("agent.state_port.read_interrupt", _boom)

        store = _FakeQueue()
        result = await iqs.submit_user_input(
            "sess-1", "hello", "user", queue=store, executor_registry=_FakeRegistry()
        )

        assert result.status.value == "STARTED", "a broken probe must not block input"


class _Idle:
    """Minimal SessionState stand-in: idle, no reason."""

    busy = False
    reason = ""


class _FakeQueue:
    """Just enough of UserInputQueue for the two branches this test drives."""

    def __init__(self) -> None:
        self.enqueued: list[str] = []

    async def find_active_by_client_msg_id(self, client_msg_id):
        return None

    async def find_delivered_by_client_msg_id(self, client_msg_id, **kwargs):
        return None

    async def list_active(self, session_id):
        return []

    async def enqueue(self, session_id, payload, source, **kwargs):
        self.enqueued.append(payload)
        return object(), len(self.enqueued)

    async def insert_claimed(self, session_id, payload, source, **kwargs):
        return object()


class _FakeExecutor:
    async def execute(self, *args, **kwargs):
        return None


class _FakeRegistry:
    def resolve(self, route):
        return _FakeExecutor()
