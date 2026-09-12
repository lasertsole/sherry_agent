"""Behavior tests for taskflow_list (GAP-9): the cross-session task board.

The tool is the global counterpart of taskflow_summary: it deliberately ignores
session/channel scoping and lists every flow in the registry, delegating to
``store_sqlite.get_all_flows_sync(status_filter)``.

Rendered table columns: flow_id, status, description (40 chars), steps
done/total, creator_session_key (16 chars), and a best-effort last-activity
timestamp. The schema has no ``updated_at`` column (GAP-9 is migration-free,
mirroring GAP-11), so the tool derives the newest persisted state timestamp
(step.dispatched_at / result.injected_at / wait.set_at) and renders ``-`` when
the flow carries none.

Tests seed flows directly in the store (no dispatch pipeline), so the board
formatter and the sync query are exercised in isolation.
"""

import asyncio
from pathlib import Path

import pytest

from agent.tools.taskflow.config import TaskFlowStatus
from agent.tools.taskflow.registry import store_sqlite
from agent.tools.taskflow.tools.taskflow_list import taskflow_list

pytestmark = [pytest.mark.unit]


def _state(
    description: str,
    *,
    creator: str = "",
    steps: list[dict] | None = None,
    results: list[dict] | None = None,
) -> dict:
    state: dict = {
        "description": description,
        "steps": list(steps or []),
        "results": list(results or []),
    }
    if creator:
        state["creator_session_key"] = creator
    return state


def _step(step_id: str, status: str, *, dispatched_at: float | None = None) -> dict:
    step: dict = {
        "step_id": step_id,
        "task": f"task {step_id}",
        "depends_on": [],
        "status": status,
    }
    if dispatched_at is not None:
        step["dispatched_at"] = dispatched_at
    return step


# ---------------------------------------------------------------------------
# Tool rendering
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_empty_registry(isolated_db: Path):
    out = await taskflow_list.coroutine()

    assert out == "No task flows found"


@pytest.mark.asyncio
async def test_list_active_flows_renders_one_row_each(isolated_db: Path):
    await store_sqlite.create_flow(
        "flow-a",
        _state(
            "deploy the service",
            creator="session-aaaaaaaaaaaa",
            steps=[
                _step("step-1", "done", dispatched_at=1000.0),
                _step("step-2", "done", dispatched_at=2000.0),
            ],
        ),
    )
    await store_sqlite.create_flow(
        "flow-b",
        _state(
            "run the test suite", creator="session-bbbbbbbbbbbb", steps=[_step("step-1", "done")]
        ),
    )
    await store_sqlite.update_flow("flow-b", 1, wait={"reason": "bump"})
    await store_sqlite.create_flow(
        "flow-c",
        _state("summarise findings"),
        status=TaskFlowStatus.WAITING.value,
    )
    await store_sqlite.update_flow("flow-c", 1, wait={"reason": "bump"})
    await store_sqlite.update_flow("flow-c", 2, wait={"reason": "bump again"})

    out = await taskflow_list.coroutine()

    rows = [line for line in out.splitlines() if line.startswith("flow-")]
    assert len(rows) == 3
    # ORDER BY expected_revision DESC: newest activity first.
    assert [row.split("|")[0].strip() for row in rows] == ["flow-c", "flow-b", "flow-a"]
    assert "running" in out
    assert "waiting" in out
    assert "2/2" in out
    assert "1/1" in out
    assert "0/0" in out
    assert "session-aaaaaaaa" in out
    assert "session-bbbbbbbb" in out


@pytest.mark.asyncio
async def test_list_default_excludes_terminal(isolated_db: Path):
    await store_sqlite.create_flow("flow-live", _state("still open"))
    await store_sqlite.create_flow(
        "flow-done", _state("finished"), status=TaskFlowStatus.DONE.value
    )

    out = await taskflow_list.coroutine()

    assert "flow-live" in out
    assert "flow-done" not in out


@pytest.mark.asyncio
async def test_list_all_includes_terminal(isolated_db: Path):
    await store_sqlite.create_flow("flow-live", _state("still open"))
    await store_sqlite.create_flow(
        "flow-done", _state("finished"), status=TaskFlowStatus.DONE.value
    )

    out = await taskflow_list.coroutine(status_filter="all")

    assert "flow-live" in out
    assert "flow-done" in out
    assert "done" in out


@pytest.mark.asyncio
async def test_list_specific_status_filter(isolated_db: Path):
    await store_sqlite.create_flow(
        "flow-w", _state("waiting one"), status=TaskFlowStatus.WAITING.value
    )
    await store_sqlite.create_flow("flow-r", _state("running one"))

    out = await taskflow_list.coroutine(status_filter="waiting")

    assert "flow-w" in out
    assert "flow-r" not in out


@pytest.mark.asyncio
async def test_list_truncates_description_and_creator(isolated_db: Path):
    long_description = "d" * 60
    long_creator = "agent:main:session:1234567890"
    await store_sqlite.create_flow("flow-long", _state(long_description, creator=long_creator))

    out = await taskflow_list.coroutine()

    assert long_description not in out
    assert "d" * 40 in out
    assert long_creator not in out
    assert long_creator[:16] in out


@pytest.mark.asyncio
async def test_list_renders_updated_at_and_dash_when_absent(isolated_db: Path):
    await store_sqlite.create_flow(
        "flow-stamped",
        _state(
            "has activity",
            steps=[_step("step-1", "done", dispatched_at=1000.0)],
            results=[{"child_session_key": "child-1", "result": "R1", "injected_at": 2000.0}],
        ),
    )
    await store_sqlite.create_flow("flow-bare", _state("no timestamps"))

    out = await taskflow_list.coroutine()

    stamped_row = next(line for line in out.splitlines() if line.startswith("flow-stamped"))
    bare_row = next(line for line in out.splitlines() if line.startswith("flow-bare"))
    # 2000 epoch seconds = 1970-01-01 00:33:20 UTC (the newest of the two).
    assert stamped_row.rstrip().endswith("1970-01-01 00:33:20")
    assert bare_row.rstrip().endswith("-")


@pytest.mark.asyncio
async def test_list_fail_open_on_db_error(isolated_db: Path, monkeypatch: pytest.MonkeyPatch):
    """A failing sync init/read must render the empty-board text, not raise."""

    def _boom() -> None:
        raise RuntimeError("db unavailable")

    monkeypatch.setattr(store_sqlite, "_ensure_tables_sync", _boom)

    out = await taskflow_list.coroutine()

    assert out == "No task flows found"


# ---------------------------------------------------------------------------
# Store query semantics (get_all_flows_sync)
# ---------------------------------------------------------------------------


def test_get_all_flows_sync_orders_by_revision_desc(isolated_db: Path):
    async def _setup() -> None:
        await store_sqlite.create_flow("flow-low", _state("low"))
        await store_sqlite.create_flow("flow-high", _state("high"))
        await store_sqlite.update_flow("flow-high", 1, wait={"reason": "bump"})

    asyncio.run(_setup())

    flows = store_sqlite.get_all_flows_sync()

    assert [flow["flow_id"] for flow in flows] == ["flow-high", "flow-low"]
    assert [flow["expected_revision"] for flow in flows] == [2, 1]


def test_get_all_flows_sync_active_all_and_specific_filters(isolated_db: Path):
    async def _setup() -> None:
        await store_sqlite.create_flow("flow-run", _state("running"))
        await store_sqlite.create_flow(
            "flow-wait", _state("waiting"), status=TaskFlowStatus.WAITING.value
        )
        await store_sqlite.update_flow("flow-wait", 1, wait={"reason": "bump"})
        await store_sqlite.create_flow(
            "flow-done", _state("done"), status=TaskFlowStatus.DONE.value
        )
        await store_sqlite.update_flow("flow-done", 1, wait={"reason": "bump"})
        await store_sqlite.update_flow("flow-done", 2, wait={"reason": "bump again"})

    asyncio.run(_setup())

    assert [flow["flow_id"] for flow in store_sqlite.get_all_flows_sync("active")] == [
        "flow-wait",
        "flow-run",
    ]
    assert [flow["flow_id"] for flow in store_sqlite.get_all_flows_sync("all")] == [
        "flow-done",
        "flow-wait",
        "flow-run",
    ]
    assert [flow["flow_id"] for flow in store_sqlite.get_all_flows_sync("done")] == ["flow-done"]


def test_get_all_flows_sync_fail_open(isolated_db: Path, monkeypatch: pytest.MonkeyPatch):
    def _boom() -> None:
        raise RuntimeError("db unavailable")

    monkeypatch.setattr(store_sqlite, "_ensure_tables_sync", _boom)

    assert store_sqlite.get_all_flows_sync() == []
    assert store_sqlite.get_all_flows_sync("all") == []
