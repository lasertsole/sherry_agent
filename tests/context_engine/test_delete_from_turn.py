"""Persisted-history truncation for message edits (unit, isolated SQLite).

The edit path truncates BOTH layers. This pins the persisted half:

* ``find_user_turn_cut`` turns the clicked row (or a turn number) into the pair
  the two layers agree on — the 1-based ordinal of USER turns (the checkpoint
  counts human messages) and the persisted turn to delete from;
* ``delete_messages_from_turn`` hard-deletes those rows so ``max(turn_num)``
  drops and the re-run reuses the edited turn's number, and re-points the tree
  leaf at the previous turn.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Generator

import pytest
from langchain_core.messages import AIMessage, HumanMessage

from context_engine.store import core as store_core
from context_engine.store.db import build_schema_v1

pytestmark = [pytest.mark.asyncio, pytest.mark.unit]

SESSION = "s-edit"


@pytest.fixture()
def store_db(monkeypatch, tmp_path) -> Generator[sqlite3.Connection]:
    """An isolated, current-schema DB wired into ``store.core`` (never the real one)."""
    conn = sqlite3.connect(
        tmp_path / "mes_memory_edit.db",
        check_same_thread=False,
        isolation_level=None,
    )
    conn.row_factory = sqlite3.Row
    build_schema_v1(conn)
    monkeypatch.setattr(store_core, "_db", conn)
    yield conn
    conn.close()


async def _seed_three_turns() -> None:
    for turn in (1, 2, 3):
        await store_core.add_messages(SESSION, [HumanMessage(f"q{turn}"), AIMessage(f"a{turn}")])


async def test_find_user_turn_cut_returns_the_checkpoint_ordinal(store_db) -> None:
    await _seed_three_turns()

    assert await _as_thread(store_core.find_user_turn_cut, SESSION, 2) == (2, 2)

    # Editing the FIRST turn: ordinal 1, cut at turn 1.
    assert await _as_thread(store_core.find_user_turn_cut, SESSION, 1) == (1, 1)


async def test_find_user_turn_cut_prefers_the_clicked_row(store_db) -> None:
    await _seed_three_turns()
    row = store_db.execute(
        "SELECT id FROM messages WHERE session_id = ? AND turn_num = 2 AND role = 'human'",
        (SESSION,),
    ).fetchone()

    assert await _as_thread(store_core.find_user_turn_cut, SESSION, 2, int(row["id"])) == (2, 2)


async def test_find_user_turn_cut_ignores_non_user_rows_and_missing_turns(store_db) -> None:
    await _seed_three_turns()
    notice = AIMessage("note")
    await store_core.add_messages(SESSION, [notice])

    assert await _as_thread(store_core.find_user_turn_cut, SESSION, 99) == (3, 3)
    assert await _as_thread(store_core.find_user_turn_cut, SESSION, 0) is None


def _as_thread(fn, *args):
    import asyncio

    return asyncio.to_thread(fn, *args)


async def test_delete_from_turn_drops_the_rows_and_repoints_the_leaf(store_db) -> None:
    await _seed_three_turns()

    deleted = store_core.delete_messages_from_turn(SESSION, 2)

    assert deleted == 4
    assert store_core.get_max_turn_num(SESSION) == 1
    # The tree leaf is the last surviving row (turn 1's answer).
    leaf = store_core.get_session_leaf(SESSION)
    assert leaf is not None
    leaf_row = store_core.get_message_by_id(SESSION, leaf)
    assert leaf_row is not None
    assert leaf_row["turn_num"] == 1
    # Turn 2 is gone, so the re-run reuses number 2 (max + 1).
    assert store_core.find_user_turn_cut(SESSION, 2) == (1, 1)


async def test_delete_from_the_first_turn_clears_the_tree(store_db) -> None:
    await _seed_three_turns()

    deleted = store_core.delete_messages_from_turn(SESSION, 1)

    assert deleted == 6
    assert store_core.get_max_turn_num(SESSION) == 0
    assert store_core.get_session_leaf(SESSION) is None
