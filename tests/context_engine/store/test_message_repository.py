"""Equivalence tests for ``context_engine.store.message_repository``.

The repository is a behaviour-preserving extraction of the ``messages``-table
reads that were inlined in ``context_engine.store.core``. These tests pin that
its results are field-for-field identical to the previous raw SQL.
"""

import pytest
from langchain_core.messages import HumanMessage

import context_engine.store.core as core
from context_engine.store import add_messages
from context_engine.store import db as store_db
from context_engine.store.message_repository import MessageRepository

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]


@pytest.fixture
def isolated_db(tmp_path, monkeypatch):
    db_path = tmp_path / "mes_memory.db"
    monkeypatch.setattr(store_db, "_db_path", db_path)
    monkeypatch.setattr(store_db, "_db", None)
    monkeypatch.setattr(core, "_db", store_db.get_db())
    return db_path


async def _seed(sid: str) -> None:
    for i in range(4):
        await add_messages(sid, [HumanMessage(content=f"turn {i}")])
    # Turn 4 is context-ineligible; turns 1-2 are compacted.
    await add_messages(
        sid,
        [HumanMessage(content="ineligible", additional_kwargs={"context_eligible": False})],
    )
    core.mark_messages_compacted(sid, up_to_turn=2)


def _raw_rows(sid: str, *, eligible: bool, compacted: bool) -> list[dict]:
    eligible_filter = " AND context_eligible = 1" if eligible else ""
    compacted_filter = "" if compacted else " AND compacted = 0"
    rows = (
        core._shared_db()
        .execute(
            "SELECT * FROM messages WHERE session_id = ? AND turn_num >= ? AND turn_num <= ?"
            f"{compacted_filter}{eligible_filter} ORDER BY turn_num DESC, id ASC",
            (sid, 1, 5),
        )
        .fetchall()
    )
    return [dict(row) for row in rows]


async def test_fetch_turn_range_matches_raw_sql(isolated_db):
    sid = "repo-range"
    await _seed(sid)
    repo = MessageRepository(core._shared_db)

    for eligible in (True, False):
        for compacted in (True, False):
            got = repo.fetch_turn_range(
                sid, 1, 5, only_eligible=eligible, include_compacted=compacted
            )
            assert [dict(row) for row in got] == _raw_rows(
                sid, eligible=eligible, compacted=compacted
            )


async def test_max_turn_num_matches_raw_sql(isolated_db):
    sid = "repo-max"
    await _seed(sid)
    repo = MessageRepository(core._shared_db)

    expected = (
        core._shared_db()
        .execute("SELECT MAX(turn_num) FROM messages WHERE session_id = ?", (sid,))
        .fetchone()[0]
    )
    assert repo.max_turn_num(sid) == expected
    assert repo.max_turn_num("no-such-session") == 0


async def test_get_by_id_matches_raw_sql(isolated_db):
    sid = "repo-by-id"
    await _seed(sid)
    repo = MessageRepository(core._shared_db)

    first_id = (
        core._shared_db()
        .execute("SELECT id FROM messages WHERE session_id = ? ORDER BY id ASC LIMIT 1", (sid,))
        .fetchone()[0]
    )
    raw = core._shared_db().execute("SELECT * FROM messages WHERE id = ?", (first_id,)).fetchone()
    assert dict(repo.get_by_id(first_id)) == dict(raw)
    assert repo.get_by_id(999999) is None


async def test_history_readers_match_decoded_repository_rows(isolated_db):
    sid = "repo-readers"
    await _seed(sid)
    repo = MessageRepository(core._shared_db)

    expected = [
        core._decode_json_columns(dict(row))
        for row in repo.fetch_turn_range(sid, 1, 5, only_eligible=True, include_compacted=False)
    ]

    assert core.get_turns_by_turn_num_scope(sid, target_turn_num=3, half_scope=10) == expected
    assert core.get_history_by_turn_page(sid, turn_page_size=10) == expected
