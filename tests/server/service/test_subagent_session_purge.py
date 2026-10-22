"""Cascade purge and boot orphan-sweep contracts."""

from __future__ import annotations

from pathlib import Path

import pytest

from agent.tools.subagent.registry import memory
from agent.tools.subagent.registry import store_sqlite as registry_store
from agent.tools.subagent.types.registry import ExecutionState, ExecutionStatus, SubagentRunRecord
from server.service import session_cleanup_service as cleanup

pytestmark = [pytest.mark.unit]


def _run(run_id: str, child: str, requester: str) -> SubagentRunRecord:
    return SubagentRunRecord(
        run_id=run_id,
        child_session_key=child,
        requester_session_key=requester,
        task="t",
        execution=ExecutionState(status=ExecutionStatus.TERMINAL),
    )


@pytest.fixture()
def registry_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(registry_store, "_DB_DIR", tmp_path)
    monkeypatch.setattr(registry_store, "_DB_PATH", tmp_path / "registry.db")
    monkeypatch.setattr(registry_store, "_initialized", False)
    monkeypatch.setattr(registry_store, "_init_loop", None)
    monkeypatch.setattr(registry_store, "_sync_tables_ready", False)
    memory.clear()
    yield
    memory.clear()


@pytest.mark.asyncio
async def test_cascade_collects_all_generations_and_leaves_siblings(registry_db, monkeypatch):
    b = _run("b", "agent:main:subagent:b", "agent:main:session:main-a")
    c = _run("c", "agent:main:subagent:c", "agent:main:subagent:b")
    e = _run("e", "agent:main:subagent:e", "agent:main:session:main-d")
    for run in (b, c, e):
        memory.set_run(run)

    purged: list[str] = []

    async def _record(session_id: str) -> None:
        purged.append(session_id)

    monkeypatch.setattr(cleanup, "purge_session_data", _record)

    result = await cleanup.purge_subagent_subtree_for_session("main-a")

    assert result["descendants"] == 2
    assert purged == ["agent:main:subagent:b", "agent:main:subagent:c"]
    assert memory.get("b") is None and memory.get("c") is None
    assert memory.get("e") is not None


@pytest.mark.asyncio
async def test_boot_sweep_removes_orphan_trees_but_keeps_live_root_children(
    monkeypatch,
):
    from server.service import orphan_session_sweep as sweep

    good = _run("good", "agent:main:subagent:good", "agent:main:session:alive")
    orphan = _run("orphan", "agent:main:subagent:orphan", "agent:main:session:deleted")
    grandchild = _run(
        "grandchild",
        "agent:main:subagent:grandchild",
        "agent:main:subagent:orphan",
    )
    missing_parent = _run(
        "missing",
        "agent:main:subagent:missing",
        "agent:main:subagent:no-such-parent",
    )
    runs = [good, orphan, grandchild, missing_parent]

    monkeypatch.setattr(sweep, "_known_main_sessions", lambda: {"alive"})
    monkeypatch.setattr("agent.tools.subagent.registry.all_runs", lambda: runs)
    removed: list[str] = []
    purged: list[str] = []

    async def _remove(run_id: str):
        removed.append(run_id)
        return None

    async def _purge(session_id: str) -> None:
        purged.append(session_id)

    monkeypatch.setattr("agent.tools.subagent.registry.remove_run", _remove)
    monkeypatch.setattr(cleanup, "purge_session_data", _purge)

    result = await sweep.sweep_orphaned_subagent_sessions()

    assert result == dict(orphan_runs=3, checkpoint_threads=3, registry_rows=3, session_dirs=1)
    assert set(removed) == {"orphan", "grandchild", "missing"}
    assert "agent:main:subagent:good" not in purged
    assert set(purged) >= {
        "agent:main:subagent:orphan",
        "agent:main:subagent:grandchild",
        "agent:main:subagent:missing",
        "deleted",
    }


@pytest.mark.asyncio
async def test_purging_a_main_session_deletes_descendant_checkpoint_threads(
    registry_db, tmp_path: Path, monkeypatch
):
    import sqlite3

    from pub.func import rand_str_to_int

    b = _run("b", "agent:main:subagent:b", "agent:main:session:main-a")
    c = _run("c", "agent:main:subagent:c", "agent:main:subagent:b")
    e = _run("e", "agent:main:subagent:e", "agent:main:session:main-d")
    for run in (b, c, e):
        memory.set_run(run)

    checkpoint_db = tmp_path / "checkpoints" / "sqlite.db"
    checkpoint_db.parent.mkdir()
    with sqlite3.connect(checkpoint_db) as conn:
        conn.execute(
            "CREATE TABLE checkpoints (thread_id TEXT, checkpoint_ns TEXT, checkpoint_id TEXT)"
        )
        conn.execute("CREATE TABLE writes (thread_id TEXT, checkpoint_ns TEXT, checkpoint_id TEXT)")
        for session in ("main-a", b.child_session_key, c.child_session_key, e.child_session_key):
            conn.execute(
                "INSERT INTO checkpoints VALUES (?, '', ?)",
                (str(rand_str_to_int(session)), f"c-{session}"),
            )

    import agent.checkpointer.async_sqlite_checkpointer as checkpoint_mod

    monkeypatch.setattr(checkpoint_mod, "SRC_DIR", tmp_path)
    monkeypatch.setattr(cleanup, "SESSIONS_DIR", tmp_path / "sessions")
    monkeypatch.setattr(cleanup, "clear_all_register_sessions", lambda *a, **k: None)
    monkeypatch.setattr(
        "context_engine.session_continuity.auto_save_on_session_end", lambda *a, **k: None
    )
    monkeypatch.setattr("context_engine.delete_messages_by_session", lambda session_id: 0)

    await cleanup.purge_session("main-a")

    with sqlite3.connect(checkpoint_db) as conn:
        remaining = {row[0] for row in conn.execute("SELECT thread_id FROM checkpoints")}
    assert remaining == {str(rand_str_to_int(e.child_session_key))}
    assert memory.get("b") is None and memory.get("c") is None
    assert memory.get("e") is not None


@pytest.mark.asyncio
async def test_boot_sweep_aborts_when_the_session_store_is_unreadable(monkeypatch):
    from server.service import orphan_session_sweep as sweep

    run = _run("orphan", "agent:main:subagent:orphan", "agent:main:session:deleted")
    removed: list[str] = []

    async def _remove(run_id: str):
        removed.append(run_id)

    monkeypatch.setattr(sweep, "_known_main_sessions", lambda: None)
    monkeypatch.setattr("agent.tools.subagent.registry.all_runs", lambda: [run])
    monkeypatch.setattr("agent.tools.subagent.registry.remove_run", _remove)

    assert await sweep.sweep_orphaned_subagent_sessions() == dict(
        orphan_runs=0, checkpoint_threads=0, registry_rows=0, session_dirs=0
    )
    assert removed == []
