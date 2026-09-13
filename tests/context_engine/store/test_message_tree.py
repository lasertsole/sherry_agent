"""P1-5: message tree, leaf pointers and forking over MesMemory.

Messages chain via parent_message_id; a session's current leaf is recorded in
session_leafs; forking points a NEW session at any node without copying rows.
"""

import pytest
from langchain_core.messages import HumanMessage

from context_engine.store import db as store_db
from context_engine.store import add_messages
from context_engine.store.core import (
    fork_from_message,
    get_message_path_to_root,
    get_session_leaf,
)

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]


@pytest.fixture
def isolated_db(tmp_path, monkeypatch):
    db_path = tmp_path / "mes_memory.db"
    monkeypatch.setattr(store_db, "_db_path", db_path)
    monkeypatch.setattr(store_db, "_db", None)
    import context_engine.store.core as core

    monkeypatch.setattr(core, "_db", store_db.get_db())
    return db_path


async def test_messages_chain_into_a_tree(isolated_db):
    sid = "p15-chain"
    await add_messages(sid, [HumanMessage(content="turn 1")])
    leaf1 = get_session_leaf(sid)
    await add_messages(sid, [HumanMessage(content="turn 2")])
    leaf2 = get_session_leaf(sid)

    assert leaf1 is not None and leaf2 is not None and leaf2 != leaf1

    path = get_message_path_to_root(sid, leaf2)
    assert len(path) == 2
    assert path[0]["parent_message_id"] is None  # root
    assert path[1]["id"] == leaf2
    assert path[1]["parent_message_id"] == leaf1


async def test_fork_points_new_session_at_any_node_without_copying(isolated_db):
    sid = "p15-fork"
    await add_messages(sid, [HumanMessage(content="root")])
    leaf1 = get_session_leaf(sid)
    await add_messages(sid, [HumanMessage(content="child")])

    fork_from_message(sid, leaf1, "p15-fork-branch")

    # The branch session reads the prefix through the shared tree.
    path = get_message_path_to_root("p15-fork-branch", leaf1)
    assert len(path) == 1
    assert path[0]["content"] == "root"

    # No rows were copied: the branch has no messages of its own.
    from context_engine.store.core import get_max_turn_num

    assert get_max_turn_num("p15-fork-branch") == 0
