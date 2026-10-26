"""Conversation-turn identity stamped onto message rows.

The message store persists per-batch ``turn_num``; the conversation turn's uuid
lives in the trajectory store's ``current_turn_id`` contextvar. A runtime hook
bridges the two without ``context_engine`` importing ``server``.
"""

from __future__ import annotations

import asyncio
import sqlite3
from collections.abc import Generator

import pytest
from langchain_core.messages import AIMessage, HumanMessage

from context_engine.store import core as store_core
from context_engine.store.db import build_schema_v1
from runtime import hooks
from server.service.trajectory_store import install_turn_id_hook, turn_scope

pytestmark = [pytest.mark.unit]


@pytest.fixture(autouse=True)
def _clean_turn_hook() -> Generator[None]:
    hooks.unregister(hooks.GET_CURRENT_TURN_ID)
    yield
    hooks.unregister(hooks.GET_CURRENT_TURN_ID)


@pytest.fixture()
def store_db(monkeypatch, tmp_path) -> Generator[sqlite3.Connection]:
    conn = sqlite3.connect(
        tmp_path / "mes_memory_turn_id.db",
        check_same_thread=False,
        isolation_level=None,
    )
    conn.row_factory = sqlite3.Row
    build_schema_v1(conn)
    monkeypatch.setattr(store_core, "_db", conn)
    yield conn
    conn.close()


def _turn_ids(conn: sqlite3.Connection, session_id: str) -> list[str]:
    return [
        row["turn_id"]
        for row in conn.execute(
            "SELECT turn_id FROM messages WHERE session_id = ? ORDER BY id", (session_id,)
        )
    ]


def test_hook_is_registered_and_reads_the_active_scope():
    install_turn_id_hook()
    resolver = hooks.resolve(hooks.GET_CURRENT_TURN_ID)
    assert callable(resolver)
    assert resolver() == ""

    with turn_scope("turn-abc", "sid"):
        assert resolver() == "turn-abc"

    assert resolver() == ""


@pytest.mark.asyncio
async def test_hook_survives_to_thread():
    """``asyncio.to_thread`` copies the turn context into the worker."""
    install_turn_id_hook()
    resolver = hooks.resolve(hooks.GET_CURRENT_TURN_ID)
    assert resolver is not None

    with turn_scope("turn-thread", "sid"):
        value = await asyncio.to_thread(lambda: resolver())

    assert value == "turn-thread"


@pytest.mark.asyncio
async def test_persist_batch_stamps_turn_id_and_turn_num(store_db):
    install_turn_id_hook()
    messages = [HumanMessage("question"), AIMessage("answer")]

    with turn_scope("turn-A", "sess"):
        await store_core.add_messages("sess", messages)

    rows = store_db.execute(
        "SELECT turn_num, turn_id FROM messages WHERE session_id = ? ORDER BY id", ("sess",)
    ).fetchall()
    assert [row["turn_id"] for row in rows] == ["turn-A", "turn-A"]
    assert messages[0].additional_kwargs["turn_id"] == "turn-A"
    assert messages[1].additional_kwargs["turn_id"] == "turn-A"
    assert messages[0].additional_kwargs["turn_num"] == rows[0]["turn_num"]


@pytest.mark.asyncio
async def test_batches_in_one_turn_share_turn_id_but_not_turn_num(store_db):
    install_turn_id_hook()

    with turn_scope("turn-shared", "sess"):
        await store_core.add_messages("sess", [HumanMessage("q")])
        await store_core.add_messages("sess", [AIMessage("a")])
        await store_core.add_messages("sess", [HumanMessage("q2")])

    rows = store_db.execute(
        "SELECT DISTINCT turn_num, turn_id FROM messages WHERE session_id = ? ORDER BY turn_num",
        ("sess",),
    ).fetchall()
    assert len({row["turn_num"] for row in rows}) == 3
    assert {row["turn_id"] for row in rows} == {"turn-shared"}


@pytest.mark.asyncio
async def test_different_scopes_get_different_turn_ids(store_db):
    install_turn_id_hook()

    with turn_scope("turn-one", "sess"):
        await store_core.add_messages("sess", [HumanMessage("one")])
    with turn_scope("turn-two", "sess"):
        await store_core.add_messages("sess", [HumanMessage("two")])

    assert _turn_ids(store_db, "sess") == ["turn-one", "turn-two"]


@pytest.mark.asyncio
async def test_no_hook_degrades_to_empty_string(store_db):
    messages = [HumanMessage("no hook")]

    await store_core.add_messages("sess", messages)

    assert _turn_ids(store_db, "sess") == [""]
    assert messages[0].additional_kwargs["turn_id"] == ""


@pytest.mark.asyncio
async def test_hook_without_a_scope_degrades_to_empty_string(store_db):
    install_turn_id_hook()

    await store_core.add_messages("sess", [HumanMessage("outside scope")])

    assert _turn_ids(store_db, "sess") == [""]


def test_old_rows_keep_null_turn_id(store_db):
    conn = store_db
    conn.execute(
        "INSERT INTO messages (session_id, turn_num, role, content, timestamp, ts_ms) "
        "VALUES (?, 1, 'human', 'legacy', '2026-01-01', 1)",
        ("sess",),
    )
    row = conn.execute(
        "SELECT turn_id FROM messages WHERE session_id = ? AND content = 'legacy'", ("sess",)
    ).fetchone()
    assert row["turn_id"] is None


def test_turn_id_is_not_unique(store_db):
    conn = store_db
    conn.execute(
        "INSERT INTO messages (session_id, turn_num, turn_id, role, content, timestamp, ts_ms) "
        "VALUES (?, 1, 'same-turn', 'human', 'a', '2026-01-01', 1)",
        ("sess",),
    )
    conn.execute(
        "INSERT INTO messages (session_id, turn_num, turn_id, role, content, timestamp, ts_ms) "
        "VALUES (?, 2, 'same-turn', 'ai', 'b', '2026-01-01', 2)",
        ("sess",),
    )
    assert (
        conn.execute(
            "SELECT COUNT(*) FROM messages WHERE session_id = ? AND turn_id = 'same-turn'",
            ("sess",),
        ).fetchone()[0]
        == 2
    )
