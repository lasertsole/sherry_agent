"""Keep-latest checkpoint pruning for one thread.

The shared per-thread primitive used by the main turn-end cleanup and by a
subagent's release path. It must keep the newest TOP-LEVEL checkpoint (the row
``aget_state`` / ``sessions_messages`` reads), never let a newer ``tools:*`` /
``model:*`` row displace it, and keep only that checkpoint's writes.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import aiosqlite
import pytest

import config
from agent.checkpointer.async_sqlite_checkpointer import prune_session_checkpoints
from agent.checkpointer.thread_safe_checkpointer import ThreadSafeAsyncSqliteSaver

pytestmark = [pytest.mark.unit]


def _seed(path: Path) -> None:
    with sqlite3.connect(path) as conn:
        conn.execute(
            "CREATE TABLE checkpoints ("
            " thread_id TEXT, checkpoint_ns TEXT, checkpoint_id TEXT, checkpoint BLOB, metadata BLOB)"
        )
        conn.execute(
            "CREATE TABLE writes ("
            " thread_id TEXT, checkpoint_ns TEXT, checkpoint_id TEXT,"
            " task_id TEXT, idx INTEGER, channel TEXT, value BLOB)"
        )
        # A: top c1/c2, then newer tools/model rows; keep must be c2.
        conn.executemany(
            "INSERT INTO checkpoints VALUES (?,?,?,?,NULL)",
            [
                ("A", "", "c1", b"1"),
                ("A", "", "c2", b"2"),
                ("A", "tools:x", "t1", b"3"),
                ("A", "model:y", "m1", b"4"),
                # B: no top-level — fallback keeps the newest row.
                ("B", "tools:z", "t2", b"5"),
                ("C", "", "c3", b"6"),
            ],
        )
        conn.executemany(
            "INSERT INTO writes VALUES (?,?,?,?,0,?,NULL)",
            [
                ("A", "", "c2", "task", "messages"),
                ("A", "tools:x", "t1", "task", "messages"),
                ("B", "tools:z", "t2", "task", "messages"),
                ("C", "", "c3", "task", "messages"),
                ("D", "", "orphan", "task", "messages"),
            ],
        )


async def _clean(thread_id: str) -> dict[str, int]:
    conn = await aiosqlite.connect(":memory:")
    saver = ThreadSafeAsyncSqliteSaver(conn)
    try:
        return await saver.aclean_thread_checkpoints(thread_id)
    finally:
        await conn.close()


@pytest.fixture()
def checkpoint_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(config, "SRC_DIR", tmp_path)
    db = tmp_path / "checkpoints" / "sqlite.db"
    db.parent.mkdir()
    _seed(db)
    return db


@pytest.mark.asyncio
async def test_keeps_the_latest_top_level_and_drops_tools_model(checkpoint_db: Path):
    result = await _clean("A")

    assert result == dict(kept=1, deleted_checkpoints=3, deleted_writes=1)
    with sqlite3.connect(checkpoint_db) as conn:
        rows = conn.execute(
            "SELECT checkpoint_ns, checkpoint_id FROM checkpoints WHERE thread_id='A'"
        ).fetchall()
        writes = conn.execute(
            "SELECT checkpoint_ns, checkpoint_id FROM writes WHERE thread_id='A'"
        ).fetchall()
    assert rows == [("", "c2")]
    assert writes == [("", "c2")]


@pytest.mark.asyncio
async def test_falls_back_to_newest_row_when_no_top_level_exists(checkpoint_db: Path):
    result = await _clean("B")

    assert result["kept"] == 1
    with sqlite3.connect(checkpoint_db) as conn:
        rows = conn.execute(
            "SELECT checkpoint_ns, checkpoint_id FROM checkpoints WHERE thread_id='B'"
        ).fetchall()
        writes = conn.execute(
            "SELECT checkpoint_ns, checkpoint_id FROM writes WHERE thread_id='B'"
        ).fetchall()
    assert rows == [("tools:z", "t2")]
    assert writes == [("tools:z", "t2")]


@pytest.mark.asyncio
async def test_other_threads_and_unrelated_writes_are_untouched(checkpoint_db: Path):
    await _clean("A")

    with sqlite3.connect(checkpoint_db) as conn:
        assert conn.execute(
            "SELECT checkpoint_id FROM checkpoints WHERE thread_id='C'"
        ).fetchall() == [("c3",)]
        assert conn.execute(
            "SELECT checkpoint_ns, checkpoint_id FROM writes WHERE thread_id='C'"
        ).fetchall() == [("", "c3")]
        # D has no checkpoint at all; its writes are not A's to clean.
        assert conn.execute("SELECT count(*) FROM writes WHERE thread_id='D'").fetchone() == (1,)


@pytest.mark.asyncio
async def test_is_idempotent(checkpoint_db: Path):
    await _clean("A")
    result = await _clean("A")

    assert result == dict(kept=1, deleted_checkpoints=0, deleted_writes=0)


@pytest.mark.asyncio
async def test_missing_database_is_a_noop(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(config, "SRC_DIR", tmp_path)

    assert await prune_session_checkpoints("no-such-session") == dict(
        kept=0, deleted_checkpoints=0, deleted_writes=0
    )


def test_hitl_interrupt_resume_survives_pruning(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """A real SQLite interrupt/resume loop still completes after the prune."""
    from typing import TypedDict

    from langgraph.checkpoint.sqlite import SqliteSaver
    from langgraph.graph import END, START, StateGraph
    from langgraph.types import Command, interrupt

    from pub.func import rand_str_to_int

    class _State(TypedDict):
        value: str

    def _ask(_state: _State) -> dict:
        answer = interrupt({"question": "pick"})
        return {"value": str(answer)}

    monkeypatch.setattr(config, "SRC_DIR", tmp_path)
    db = tmp_path / "checkpoints" / "sqlite.db"
    db.parent.mkdir()
    graph_builder = StateGraph(_State)
    graph_builder.add_node("ask", _ask)
    graph_builder.add_edge(START, "ask")
    graph_builder.add_edge("ask", END)

    with SqliteSaver.from_conn_string(str(db)) as saver:
        graph = graph_builder.compile(checkpointer=saver)
        thread = "t-hitl-prune"
        config_ = {"configurable": {"thread_id": thread}}
        first = graph.invoke({"value": ""}, config_)
        assert "__interrupt__" in first

        import asyncio

        asyncio.run(_clean(str(rand_str_to_int(thread))))

        resumed = graph.invoke(Command(resume="approved"), config_)
        assert resumed["value"] == "approved"
