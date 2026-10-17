"""Flow lineage: ``resumed_from`` / ``superseded_by`` and the active-read filter.

A flow that replaces an earlier one records the pair — the successor's
``resumed_from`` pointer and the predecessor's ``superseded_by`` back-pointer —
so the evolution is traceable, while the replaced flow stops counting as an
active flow (the continuation enforcer, the progress panel and the per-session
flow cap all read through ``get_active_flows_sync``). The columns are an
additive migration: a pre-lineage database converges on its next open and its
rows read NULL (= no link).
"""

import sqlite3
from pathlib import Path

import pytest

from agent.tools.taskflow.config import TABLE_NAME, TaskFlowStatus
from agent.tools.taskflow.registry import store_sqlite
from agent.tools.taskflow.tools.taskflow_create import taskflow_create
from agent.tools.taskflow.tools.taskflow_summary import taskflow_summary

pytestmark = [pytest.mark.unit]

_SESSION = "sess-lineage"
_PRE_LINEAGE_CREATE_TABLE_SQL = f"""
CREATE TABLE {TABLE_NAME} (
    flow_id TEXT PRIMARY KEY,
    state_json TEXT NOT NULL,
    wait_json TEXT,
    expected_revision INTEGER NOT NULL,
    status TEXT NOT NULL,
    child_session_key TEXT,
    total_tokens INTEGER DEFAULT 0,
    total_cost REAL DEFAULT 0.0,
    token_budget INTEGER DEFAULT 0,
    deadline_ts REAL,
    session_id TEXT NOT NULL DEFAULT ''
);
"""


async def _create(flow_id: str, *, resumed_from: str | None = None) -> str:
    return await taskflow_create.coroutine(
        session_id=_SESSION,
        flow_id=flow_id,
        description=f"lineage probe {flow_id}",
        resumed_from=resumed_from,
    )


def _columns(db_path: Path) -> set[str]:
    conn = sqlite3.connect(db_path)
    try:
        return {row[1] for row in conn.execute(f"PRAGMA table_info({TABLE_NAME})")}
    finally:
        conn.close()


@pytest.mark.asyncio
async def test_create_without_a_predecessor_leaves_both_pointers_null(isolated_db: Path):
    out = await _create("flow-solo")

    assert out.startswith("TaskFlow created"), out
    flow = await store_sqlite.get_flow("flow-solo", _SESSION)
    assert flow is not None
    assert flow["resumed_from"] is None
    assert flow["superseded_by"] is None


@pytest.mark.asyncio
async def test_replacing_a_flow_stamps_both_pointers(isolated_db: Path):
    assert (await _create("flow-old")).startswith("TaskFlow created")

    out = await _create("flow-new", resumed_from="flow-old")

    assert out.startswith("TaskFlow created"), out
    assert "resumed_from=flow-old" in out

    successor = await store_sqlite.get_flow("flow-new", _SESSION)
    assert successor is not None
    assert successor["resumed_from"] == "flow-old"
    assert successor["superseded_by"] is None

    predecessor = await store_sqlite.get_flow("flow-old", _SESSION)
    assert predecessor is not None
    assert predecessor["superseded_by"] == "flow-new"
    assert predecessor["resumed_from"] is None
    # Lineage is metadata: the predecessor's content and revision are untouched.
    assert predecessor["expected_revision"] == 1
    assert predecessor["status"] == TaskFlowStatus.RUNNING.value


@pytest.mark.asyncio
async def test_a_superseded_flow_leaves_the_active_read_but_stays_on_the_board(
    isolated_db: Path,
):
    await _create("flow-doomed")
    assert [f["flow_id"] for f in store_sqlite.get_active_flows_sync(_SESSION)] == ["flow-doomed"]

    await _create("flow-heir", resumed_from="flow-doomed")

    active = [f["flow_id"] for f in store_sqlite.get_active_flows_sync(_SESSION)]
    assert active == ["flow-heir"]
    # The history view still shows it, with the back-pointer attached.
    board = {f["flow_id"]: f for f in store_sqlite.get_all_flows_sync(_SESSION, "all")}
    assert set(board) == {"flow-doomed", "flow-heir"}
    assert board["flow-doomed"]["superseded_by"] == "flow-heir"


@pytest.mark.asyncio
async def test_unknown_predecessor_is_refused_without_creating_anything(isolated_db: Path):
    out = await _create("flow-orphan", resumed_from="flow-missing")

    assert out.startswith("Error:"), out
    assert "flow-missing" in out
    assert await store_sqlite.get_flow("flow-orphan", _SESSION) is None


@pytest.mark.asyncio
async def test_a_self_referential_lineage_is_refused(isolated_db: Path):
    out = await _create("flow-ouroboros", resumed_from="flow-ouroboros")

    assert out.startswith("Error:"), out
    assert await store_sqlite.get_flow("flow-ouroboros", _SESSION) is None


@pytest.mark.asyncio
async def test_summary_names_the_lineage(isolated_db: Path):
    await _create("flow-ancestor")
    await _create("flow-descendant", resumed_from="flow-ancestor")

    descendant = await taskflow_summary.coroutine(flow_id="flow-descendant", session_id=_SESSION)
    ancestor = await taskflow_summary.coroutine(flow_id="flow-ancestor", session_id=_SESSION)

    assert "resumed_from: flow-ancestor" in descendant
    assert "superseded_by: flow-descendant" in ancestor


@pytest.mark.asyncio
async def test_mark_superseded_is_session_scoped(isolated_db: Path):
    await _create("flow-mine")

    marked = await store_sqlite.mark_flow_superseded(
        "flow-mine", "flow-elsewhere", session_id="some-other-session"
    )

    assert marked is False
    flow = await store_sqlite.get_flow("flow-mine", _SESSION)
    assert flow is not None
    assert flow["superseded_by"] is None


@pytest.mark.asyncio
async def test_the_lineage_columns_migrate_a_legacy_database(isolated_db: Path):
    conn = sqlite3.connect(isolated_db)
    try:
        conn.execute(_PRE_LINEAGE_CREATE_TABLE_SQL)
        conn.execute(
            f"INSERT INTO {TABLE_NAME} "
            "(flow_id, state_json, wait_json, expected_revision, status, "
            "child_session_key, session_id) "
            "VALUES ('flow-legacy', '{}', NULL, 1, ?, NULL, ?)",
            (TaskFlowStatus.RUNNING.value, _SESSION),
        )
        conn.commit()
    finally:
        conn.close()
    assert "resumed_from" not in _columns(isolated_db)

    await store_sqlite.ensure_db()

    assert {"resumed_from", "superseded_by"} <= _columns(isolated_db)
    legacy = await store_sqlite.get_flow("flow-legacy", _SESSION)
    assert legacy is not None
    assert legacy["resumed_from"] is None
    assert legacy["superseded_by"] is None
