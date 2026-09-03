"""Regression tests for atomic turn assignment in ``add_messages`` (audit #5).

Audit #5: ``add_messages`` read ``MAX(turn_num)`` outside any lock/transaction
and then inserted with ``turn = max + 1``. Two concurrent calls for the same
session could both observe the same MAX and silently merge two turns into one
(duplicated ``turn_num`` — corrupted history ordering and pagination).

Fix: a module-level ``threading.Lock`` (``store.core._turn_assign_lock``)
serializes the re-read + re-tag + INSERT critical section.

Teeth: the concurrency test monkeypatches ``get_max_turn_num`` to sleep after
reading, widening the read→write window. On the pre-fix code both threads read
the same MAX inside their overlapping sleep windows and both batches land on
the same ``turn_num`` (test fails); with the lock, the second writer blocks at
the lock until the first has inserted, so turns stay distinct (test passes).
"""

from __future__ import annotations

import asyncio
import sqlite3
import threading
import time
from collections.abc import Generator

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from context_engine.store import core as store_core
from context_engine.store.db import _migrate

pytestmark = pytest.mark.unit


def _turns(conn: sqlite3.Connection, session_id: str) -> list[int]:
    """Distinct ``turn_num`` values of a session, ascending."""
    rows = conn.execute(
        "SELECT DISTINCT turn_num FROM messages WHERE session_id = ? ORDER BY turn_num",
        (session_id,),
    ).fetchall()
    return [row["turn_num"] for row in rows]


@pytest.fixture()
def store_db(monkeypatch, tmp_path) -> Generator[sqlite3.Connection, None, None]:
    """An isolated, fully migrated DB wired into ``store.core._db``.

    Same pattern as ``test_store_origin.py``: mirrors ``get_db()``'s key
    settings (row_factory, autocommit) against a throwaway file so the
    production DB is never touched. ``store/core.py`` resolves ``_db`` at call
    time from its module globals, so patching the attribute redirects every
    store function in the module — including worker threads.
    """
    conn = sqlite3.connect(
        tmp_path / "mes_memory_test.db",
        check_same_thread=False,
        isolation_level=None,
    )
    conn.row_factory = sqlite3.Row
    _migrate(conn)
    monkeypatch.setattr(store_core, "_db", conn)
    yield conn
    conn.close()


class TestConcurrentTurnAssignment:
    """The audit #5 regression: concurrent same-session writers."""

    def test_concurrent_add_messages_assign_distinct_turn_nums(self, store_db, monkeypatch):
        """Two concurrent ``add_messages`` calls on one session must not share a turn.

        Teeth: ``get_max_turn_num`` is patched to sleep 0.2s after reading, so
        on the pre-fix code both callers read the same MAX before either
        inserts and both batches merge onto a single ``turn_num``.
        """
        original = store_core.get_max_turn_num

        def slow_max(session_id: str) -> int:
            value = original(session_id)
            time.sleep(0.2)  # widen the read→write race window
            return value

        monkeypatch.setattr(store_core, "get_max_turn_num", slow_max)

        barrier = threading.Barrier(2)
        errors: list[BaseException] = []

        def run(batch: int) -> None:
            try:
                barrier.wait(timeout=5)
                asyncio.run(
                    store_core.add_messages(
                        "s_race",
                        [HumanMessage(f"batch {batch}"), AIMessage(f"reply {batch}")],
                    )
                )
            except BaseException as exc:  # pragma: no cover - surfaces thread crashes
                errors.append(exc)

        threads = [threading.Thread(target=run, args=(i,)) for i in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)

        assert not errors, f"add_messages raised in worker threads: {errors}"
        assert _turns(store_db, "s_race") == [1, 2]


class TestTurnSemantics:
    """Sanity: the fix preserves single-writer turn semantics."""

    @pytest.mark.asyncio
    async def test_sequential_calls_increment_turn_nums(self, store_db):
        """Each ``add_messages`` call starts a new turn: 1, 2, 3."""
        await store_core.add_messages("s_seq", [HumanMessage("q1")])
        await store_core.add_messages("s_seq", [AIMessage("a1")])
        await store_core.add_messages("s_seq", [HumanMessage("q2")])

        assert _turns(store_db, "s_seq") == [1, 2, 3]

    @pytest.mark.asyncio
    async def test_batch_shares_single_turn_num(self, store_db):
        """All messages of one call share the same ``turn_num``."""
        await store_core.add_messages(
            "s_batch",
            [
                HumanMessage("q"),
                AIMessage("a"),
                ToolMessage(content="t", name="read", tool_call_id="call_1"),
            ],
        )

        assert _turns(store_db, "s_batch") == [1]
