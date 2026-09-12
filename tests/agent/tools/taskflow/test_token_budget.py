"""Unit tests for GAP-3 token/cost budget tracking.

Covers three seams:

1. the additive ``task_flows`` migration (``_ensure_token_columns``) on a
   pre-existing database that lacks the token columns;
2. ``taskflow_resume`` aggregating a child session's ``token_usage`` into the
   flow's ``total_tokens``/``total_cost`` (and skipping it when absent);
3. the ``taskflow_budget`` tool's query rendering (ok / WARNING / EXCEEDED)
   and its ``set`` action guards (positive int, optimistic lock, terminal).

The store is isolated per test through the ``isolated_db`` fixture: the real
``taskflow_registry.db`` is never touched.
"""

from pathlib import Path

import aiosqlite
import pytest

from agent.tools.taskflow.config import INITIAL_REVISION, TABLE_NAME, TaskFlowStatus
from agent.tools.taskflow.registry import store_sqlite
from agent.tools.taskflow.registry.store_sqlite import _ensure_token_columns
from agent.tools.taskflow.tools.taskflow_budget import taskflow_budget
from agent.tools.taskflow.tools.taskflow_resume import taskflow_resume

pytestmark = [pytest.mark.unit]


def _make_state(description: str = "demo flow") -> dict:
    return {"description": description, "steps": [], "results": []}


async def _create(flow_id: str, **kwargs) -> dict:
    return await store_sqlite.create_flow(flow_id, _make_state(flow_id), **kwargs)


# ---------------------------------------------------------------------------
# Migration
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_migration_adds_columns(isolated_db: Path):
    """A legacy DB without token columns gains them with zero defaults."""
    legacy_schema = f"""
    CREATE TABLE {TABLE_NAME} (
        flow_id TEXT PRIMARY KEY,
        state_json TEXT NOT NULL,
        wait_json TEXT,
        expected_revision INTEGER NOT NULL,
        status TEXT NOT NULL,
        child_session_key TEXT
    );
    """
    async with aiosqlite.connect(isolated_db) as db:
        await db.execute(legacy_schema)
        await db.execute(
            f"INSERT INTO {TABLE_NAME} "
            "(flow_id, state_json, expected_revision, status) VALUES (?, ?, ?, ?)",
            ("legacy", "{}", INITIAL_REVISION, TaskFlowStatus.RUNNING.value),
        )
        await db.commit()

        await _ensure_token_columns(db)
        await _ensure_token_columns(db)  # idempotent: second run must not raise
        await db.commit()

        async with db.execute(f"PRAGMA table_info({TABLE_NAME})") as cursor:
            columns = {row[1] for row in await cursor.fetchall()}
        async with db.execute(
            f"SELECT total_tokens, total_cost, token_budget FROM {TABLE_NAME} WHERE flow_id = ?",
            ("legacy",),
        ) as cursor:
            row = await cursor.fetchone()

    assert {"total_tokens", "total_cost", "token_budget"} <= columns
    assert row == (0, 0.0, 0)


# ---------------------------------------------------------------------------
# Store: read/write of the token columns
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_row_to_flow_has_token_fields(isolated_db: Path):
    flow = await _create("flow-1")

    assert flow["total_tokens"] == 0
    assert flow["total_cost"] == 0.0
    assert flow["token_budget"] == 0

    loaded = await store_sqlite.get_flow("flow-1")
    assert loaded is not None
    assert loaded["total_tokens"] == 0
    assert loaded["total_cost"] == 0.0
    assert loaded["token_budget"] == 0


@pytest.mark.asyncio
async def test_update_flow_sets_total_tokens(isolated_db: Path):
    await _create("flow-1")

    updated = await store_sqlite.update_flow(
        "flow-1",
        INITIAL_REVISION,
        total_tokens=500,
        total_cost=1.25,
        token_budget=1000,
    )

    assert updated["total_tokens"] == 500
    assert updated["total_cost"] == 1.25
    assert updated["token_budget"] == 1000

    loaded = await store_sqlite.get_flow("flow-1")
    assert loaded is not None
    assert loaded["total_tokens"] == 500
    assert loaded["total_cost"] == 1.25
    assert loaded["token_budget"] == 1000


# ---------------------------------------------------------------------------
# Resume aggregation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_resume_aggregates_tokens(isolated_db: Path):
    await _create("flow-1")

    out = await taskflow_resume.coroutine(
        flow_id="flow-1",
        child_session_key="agent:main:subagent:child-1",
        result="done",
        token_usage={"input_tokens": 5000, "output_tokens": 2000, "model_name": "glm-5"},
    )

    assert "TaskFlow resumed" in out
    flow = await store_sqlite.get_flow("flow-1")
    assert flow is not None
    assert flow["total_tokens"] == 7000
    assert flow["total_cost"] == pytest.approx(0.0055)
    assert flow["expected_revision"] == INITIAL_REVISION + 1


@pytest.mark.asyncio
async def test_resume_without_token_usage(isolated_db: Path):
    await _create("flow-1")

    out = await taskflow_resume.coroutine(
        flow_id="flow-1",
        child_session_key="agent:main:subagent:child-1",
        result="done",
    )

    assert "TaskFlow resumed" in out
    flow = await store_sqlite.get_flow("flow-1")
    assert flow is not None
    assert flow["total_tokens"] == 0
    assert flow["total_cost"] == 0.0


@pytest.mark.asyncio
async def test_resume_calculates_cost(isolated_db: Path):
    await _create("flow-1")

    await taskflow_resume.coroutine(
        flow_id="flow-1",
        child_session_key="agent:main:subagent:child-1",
        result="done",
        token_usage={
            "input_tokens": 1_000_000,
            "output_tokens": 1_000_000,
            "model_name": "deepseek-chat",
        },
    )

    flow = await store_sqlite.get_flow("flow-1")
    assert flow is not None
    assert flow["total_tokens"] == 2_000_000
    # deepseek-chat: input 0.14 + output 0.28 per 1M tokens.
    assert flow["total_cost"] == pytest.approx(0.42)


# ---------------------------------------------------------------------------
# Budget query
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_budget_query_no_budget(isolated_db: Path):
    await _create("flow-1")

    out = await taskflow_budget.coroutine(flow_id="flow-1", action="query")

    assert "no budget set" in out
    assert "status: ok" in out


@pytest.mark.asyncio
async def test_budget_query_with_budget(isolated_db: Path):
    await _create("flow-1")
    await store_sqlite.update_flow("flow-1", INITIAL_REVISION, token_budget=10000)
    await store_sqlite.update_flow("flow-1", INITIAL_REVISION + 1, total_tokens=5000)

    out = await taskflow_budget.coroutine(flow_id="flow-1", action="query")

    assert "5,000 / 10,000" in out
    assert "50.0%" in out
    assert "remaining: 5,000 tokens" in out
    assert "status: ok" in out


@pytest.mark.asyncio
async def test_budget_query_exceeded(isolated_db: Path):
    await _create("flow-1")
    await store_sqlite.update_flow("flow-1", INITIAL_REVISION, token_budget=1000)
    await store_sqlite.update_flow("flow-1", INITIAL_REVISION + 1, total_tokens=1000)

    out = await taskflow_budget.coroutine(flow_id="flow-1", action="query")

    assert "status: EXCEEDED" in out


@pytest.mark.asyncio
async def test_budget_query_warning(isolated_db: Path):
    await _create("flow-1")
    await store_sqlite.update_flow("flow-1", INITIAL_REVISION, token_budget=10000)
    await store_sqlite.update_flow("flow-1", INITIAL_REVISION + 1, total_tokens=8000)

    out = await taskflow_budget.coroutine(flow_id="flow-1", action="query")

    assert "80.0%" in out
    assert "status: WARNING" in out


# ---------------------------------------------------------------------------
# Budget set
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_budget_set(isolated_db: Path):
    await _create("flow-1")

    out = await taskflow_budget.coroutine(flow_id="flow-1", action="set", token_budget=50000)

    assert "Budget set" in out
    assert "token_budget=50,000" in out
    flow = await store_sqlite.get_flow("flow-1")
    assert flow is not None
    assert flow["token_budget"] == 50000
    assert flow["expected_revision"] == INITIAL_REVISION + 1


@pytest.mark.asyncio
async def test_budget_set_requires_revision(isolated_db: Path):
    await _create("flow-1")
    await store_sqlite.update_flow("flow-1", INITIAL_REVISION, token_budget=100)

    out = await taskflow_budget.coroutine(
        flow_id="flow-1",
        action="set",
        token_budget=200,
        expected_revision=INITIAL_REVISION,  # stale
    )

    assert "Error:" in out
    assert "conflict" in out.lower()
    assert "latest revision=2" in out
    flow = await store_sqlite.get_flow("flow-1")
    assert flow is not None
    assert flow["token_budget"] == 100  # rejected write left it untouched


@pytest.mark.asyncio
async def test_budget_set_terminal_rejected(isolated_db: Path):
    await _create("flow-1")
    await store_sqlite.update_flow("flow-1", INITIAL_REVISION, status=TaskFlowStatus.DONE.value)

    out = await taskflow_budget.coroutine(flow_id="flow-1", action="set", token_budget=50000)

    assert "terminal" in out
    flow = await store_sqlite.get_flow("flow-1")
    assert flow is not None
    assert flow["token_budget"] == 0
