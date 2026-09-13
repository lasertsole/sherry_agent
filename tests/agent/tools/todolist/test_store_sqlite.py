"""Unit tests for the todolist SQLite persistence layer (todos table).

The store is a module-level function API around module constants, so tests
isolate via monkeypatched ``_DB_DIR``/``_DB_PATH`` + reset once-per-process init
state (see conftest.py); the real data directory is never touched.

Coverage: replace_all/get_todos round-trip ordered by position, session
isolation, sync==async parity, empty-list clearing, field fidelity, flow
filtering, concurrent two-session writes (busy_timeout/WAL), default
degradation, replace (not append) semantics, and the no-event-loop sync path.
"""

import asyncio
from pathlib import Path

import pytest

from agent.tools.todolist.registry import store_sqlite

pytestmark = [pytest.mark.unit]


def _todo(content: str, *, position: int = 0, **overrides: object) -> dict:
    todo: dict = {"content": content, "position": position}
    todo.update(overrides)
    return todo


@pytest.mark.asyncio
async def test_replace_all_and_get_todos_roundtrip_ordered_by_position(isolated_db: Path):
    todos = [
        _todo("third", position=2),
        _todo("first", position=0),
        _todo("second", position=1),
    ]

    await store_sqlite.replace_all("sess-1", todos)
    loaded = await store_sqlite.get_todos("sess-1")

    assert [t["content"] for t in loaded] == ["first", "second", "third"]
    assert [t["position"] for t in loaded] == [0, 1, 2]
    assert [t["session_id"] for t in loaded] == ["sess-1", "sess-1", "sess-1"]


@pytest.mark.asyncio
async def test_second_session_is_isolated(isolated_db: Path):
    await store_sqlite.replace_all("sess-a", [_todo("a", position=0)])
    await store_sqlite.replace_all("sess-b", [_todo("b", position=0), _todo("b2", position=1)])

    a = await store_sqlite.get_todos("sess-a")
    b = await store_sqlite.get_todos("sess-b")

    assert [t["content"] for t in a] == ["a"]
    assert [t["content"] for t in b] == ["b", "b2"]
    assert await store_sqlite.get_todos("sess-missing") == []


@pytest.mark.asyncio
async def test_get_todos_sync_matches_async_path(isolated_db: Path):
    await store_sqlite.replace_all(
        "sess-1",
        [
            _todo("x", position=0, status="in_progress", category="deep"),
            _todo("y", position=1, flow_id="flow-1", step_id="step-1"),
        ],
    )

    async_rows = await store_sqlite.get_todos("sess-1")
    sync_rows = store_sqlite.get_todos_sync("sess-1")

    assert sync_rows == async_rows
    assert store_sqlite.get_todos_sync("sess-missing") == []


@pytest.mark.asyncio
async def test_replace_all_empty_list_clears(isolated_db: Path):
    await store_sqlite.replace_all("sess-1", [_todo("x", position=0), _todo("y", position=1)])
    assert len(await store_sqlite.get_todos("sess-1")) == 2

    await store_sqlite.replace_all("sess-1", [])

    assert await store_sqlite.get_todos("sess-1") == []
    assert store_sqlite.get_todos_sync("sess-1") == []


@pytest.mark.asyncio
async def test_status_priority_category_delegation_stored_as_given(isolated_db: Path):
    await store_sqlite.replace_all(
        "sess-1",
        [
            _todo(
                "delegated",
                position=0,
                status="completed",
                priority="high",
                category="ultrabrain",
                delegation="subagent",
                subagent_id="agent:main:subagent:child-1",
                flow_id="flow-1",
                step_id="step-2",
                plan_ref=".omo/plans/todolist-phase1.md",
            )
        ],
    )

    row = (await store_sqlite.get_todos("sess-1"))[0]

    assert row["status"] == "completed"
    assert row["priority"] == "high"
    assert row["category"] == "ultrabrain"
    assert row["delegation"] == "subagent"
    assert row["subagent_id"] == "agent:main:subagent:child-1"
    assert row["flow_id"] == "flow-1"
    assert row["step_id"] == "step-2"
    assert row["plan_ref"] == ".omo/plans/todolist-phase1.md"
    assert row["created_at"] is not None


@pytest.mark.asyncio
async def test_omitted_fields_get_defaults(isolated_db: Path):
    await store_sqlite.replace_all("sess-1", [{"content": "bare", "position": 0}])

    row = (await store_sqlite.get_todos("sess-1"))[0]

    assert row["status"] == "pending"
    assert row["priority"] == "medium"
    assert row["category"] == "quick"
    assert row["delegation"] == "self"
    assert row["subagent_id"] is None
    assert row["flow_id"] is None
    assert row["step_id"] is None
    assert row["plan_ref"] is None


@pytest.mark.asyncio
async def test_get_todos_by_flow_filters(isolated_db: Path):
    await store_sqlite.replace_all(
        "sess-1",
        [
            _todo("a", position=0, flow_id="flow-1", step_id="step-1"),
            _todo("b", position=1, flow_id="flow-1", step_id="step-2"),
            _todo("c", position=2, flow_id="flow-2"),
            _todo("d", position=3),
        ],
    )

    linked = await store_sqlite.get_todos_by_flow("sess-1", "flow-1")

    assert [t["content"] for t in linked] == ["a", "b"]
    assert [t["step_id"] for t in linked] == ["step-1", "step-2"]
    assert await store_sqlite.get_todos_by_flow("sess-1", "no-such-flow") == []
    # Flow filtering is session-scoped: the same flow id under another session is invisible.
    await store_sqlite.replace_all("sess-2", [_todo("e", position=0, flow_id="flow-1")])
    assert [t["content"] for t in await store_sqlite.get_todos_by_flow("sess-1", "flow-1")] == [
        "a",
        "b",
    ]


@pytest.mark.asyncio
async def test_replace_all_replaces_previous_not_appends(isolated_db: Path):
    await store_sqlite.replace_all("sess-1", [_todo("old", position=0), _todo("old2", position=1)])

    await store_sqlite.replace_all("sess-1", [_todo("new", position=0)])

    loaded = await store_sqlite.get_todos("sess-1")
    assert [t["content"] for t in loaded] == ["new"]
    assert len(loaded) == 1


@pytest.mark.asyncio
async def test_missing_content_rejected(isolated_db: Path):
    with pytest.raises(ValueError):
        await store_sqlite.replace_all("sess-1", [{"position": 0}])


@pytest.mark.asyncio
async def test_concurrent_two_session_writes_do_not_lock(isolated_db: Path):
    """Two sessions writing concurrently must wait on busy_timeout, never raise
    ``sqlite3.OperationalError: database is locked``."""

    async def write(session: str) -> None:
        for round_no in range(5):
            await store_sqlite.replace_all(
                session,
                [_todo(f"{session}-{round_no}-{i}", position=i) for i in range(10)],
            )

    results = await asyncio.gather(write("sess-a"), write("sess-b"), return_exceptions=True)

    failures = [r for r in results if isinstance(r, BaseException)]
    assert not failures, failures

    a = await store_sqlite.get_todos("sess-a")
    b = await store_sqlite.get_todos("sess-b")
    assert [t["content"] for t in a] == [f"sess-a-4-{i}" for i in range(10)]
    assert [t["content"] for t in b] == [f"sess-b-4-{i}" for i in range(10)]


@pytest.mark.asyncio
async def test_wal_journal_mode_is_enabled(isolated_db: Path):
    import aiosqlite

    await store_sqlite.replace_all("sess-1", [_todo("x", position=0)])

    async with aiosqlite.connect(isolated_db) as db:
        async with db.execute("PRAGMA journal_mode") as cursor:
            row = await cursor.fetchone()
    assert row is not None
    assert str(row[0]).lower() == "wal"


def test_get_todos_sync_without_event_loop(isolated_db: Path):
    """The stdlib sqlite3 sync path works with no running event loop."""

    async def _setup() -> None:
        await store_sqlite.replace_all("sess-sync", [_todo("sync", position=0)])

    asyncio.run(_setup())

    rows = store_sqlite.get_todos_sync("sess-sync")
    assert [t["content"] for t in rows] == ["sync"]
    assert rows[0]["status"] == "pending"
