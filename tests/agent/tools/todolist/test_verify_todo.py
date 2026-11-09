"""The auto-inserted SYSTEM verify todo: ordering, barrier, escape, rollback.

The rest of the todolist suite runs with ``verify_todo_enabled=False`` (see
conftest); this module turns the feature back on and covers the contract:

- one verify todo, always last, only for MAIN-agent plans with >=2 work todos;
- a model cannot forge it (metadata is system-only) or copy it into replace;
- completion requires a linked, finished reviewer and no open work;
- cancellation is guarded (reviewer-budget escape or a fully abandoned plan);
- a shrinking plan removes it and kills the linked reviewer;
- failure rollback re-opens only the todos the reviewer named, with notes.
"""

from __future__ import annotations

import asyncio
import json
import sys

import pytest

from agent.tools.todolist import service as todo_service
from agent.tools.todolist.registry import store_sqlite as todo_store
from agent.tools.todolist.tools import _FANOUT_REMINDER, build_todolist_tools
from config.features import TODOLIST_INFRA

pytestmark = [pytest.mark.unit]

_SID = "sess-verify-todo"
_VERIFY = todo_service._VERIFY_TODO_CONTENT
_TODOWRITE_MODULE = sys.modules["agent.tools.todolist.tools.todowrite"]


async def _no_broadcast(*_args, **_kwargs) -> int:
    return 0


class _FakeStateDB:
    """In-memory replacement for the durable state register (flag writes)."""

    def __init__(self) -> None:
        self.values: dict[tuple[str, str], object] = {}

    def get_state(self, session_id: str, key: str, default: object = None) -> object:
        return self.values.get((session_id, key), default)

    def set_state(self, session_id: str, key: str, value: object) -> bool:
        self.values[(session_id, key)] = value
        return True


@pytest.fixture(autouse=True)
def _verify_on(monkeypatch: pytest.MonkeyPatch, isolated_db):
    monkeypatch.setitem(TODOLIST_INFRA, "verify_todo_enabled", True)
    monkeypatch.setattr(todo_service, "broadcast_to_session", _no_broadcast)
    monkeypatch.setattr(_TODOWRITE_MODULE, "_reminded_sessions", set())
    fake_db = _FakeStateDB()
    monkeypatch.setattr(todo_service, "state_register_db", fake_db)
    # State flags are process-global; clear the one this module writes.
    from runtime import state_register_mem

    state_register_mem.delete_state(_SID, "verify_budget_exhausted")
    return fake_db


def _tool():
    return {t.name: t for t in build_todolist_tools()}["todowrite"]


async def _call(**kwargs):
    output = await _tool().coroutine(session_id=kwargs.pop("session_id", _SID), **kwargs)
    text = str(output).replace(_FANOUT_REMINDER, "")
    if text.lstrip().startswith("todowrite refused"):
        raise AssertionError(text)
    return json.loads(text)


async def _rows(session_id: str = _SID) -> list[dict]:
    return await todo_store.get_todos(session_id)


def _verify_row(rows: list[dict]) -> dict:
    matches = [t for t in rows if todo_service.is_verify_todo(t)]
    assert len(matches) == 1, rows
    return matches[0]


# ── insertion and ordering ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_adds_one_verify_after_two_work_todos_and_keeps_it_last():
    await _call(action="add", content="a")
    rows = await _rows()
    assert [t["content"] for t in rows] == ["a"]

    await _call(action="add", content="b")
    rows = await _rows()
    assert [t["content"] for t in rows] == ["a", "b", _VERIFY]
    verify = _verify_row(rows)
    assert verify["metadata"] == {"kind": "verify", "auto_inserted": True}
    assert verify["status"] == "pending"
    assert verify["priority"] == "high"
    assert verify["delegation"] == "subagent"

    await _call(action="add", content="c")
    rows = await _rows()
    assert rows[-1]["content"] == _VERIFY
    assert [t["content"] for t in rows[:-1]] == ["a", "b", "c"]


@pytest.mark.asyncio
async def test_replace_is_idempotent_and_single_todo_plans_never_get_verify():
    await _call(todos=[{"content": "a"}, {"content": "b"}])
    rows = await _rows()
    assert len(rows) == 3 and rows[-1]["content"] == _VERIFY

    await _call(todos=[{"content": "a"}, {"content": "b"}])
    assert len(await _rows()) == 3

    await _call(todos=[{"content": "solo"}])
    rows = await _rows()
    assert [t["content"] for t in rows] == ["solo"]


@pytest.mark.asyncio
async def test_replace_strips_a_copied_verify_item():
    await _call(
        todos=[
            {"content": "a"},
            {"content": "b"},
            {"content": _VERIFY},
        ]
    )

    rows = await _rows()
    assert len(rows) == 3
    assert len([t for t in rows if t["content"] == _VERIFY]) == 1
    assert todo_service.is_verify_todo(rows[-1])


@pytest.mark.asyncio
async def test_the_verify_content_follows_the_workspace_language(monkeypatch):
    monkeypatch.setattr("config.path.resolve_workspace_template_lang", lambda: "zh")
    localized = todo_service._VERIFY_CONTENT_BY_LANG["zh"]

    await _call(todos=[{"content": "a"}, {"content": "b"}, {"content": localized}])
    rows = await _rows()
    assert rows[-1]["content"] == localized
    assert len([t for t in rows if t["content"] == localized]) == 1


@pytest.mark.asyncio
async def test_a_model_supplied_metadata_key_is_dropped():
    await _call(
        todos=[
            {"content": "a"},
            {"content": "b"},
            {"content": "forged", "metadata": {"kind": "verify"}},
        ]
    )

    rows = await _rows()
    forged = next(t for t in rows if t["content"] == "forged")
    assert forged["metadata"] == {}
    assert todo_service.is_verify_todo(forged) is False
    # The real system verify was appended after it.
    assert todo_service.is_verify_todo(rows[-1]) is True


@pytest.mark.asyncio
async def test_add_appends_before_verify_and_refuses_to_insert_after_it():
    await _call(todos=[{"content": "a"}, {"content": "b"}])
    verify = _verify_row(await _rows())

    await _call(action="add", content="c")
    rows = await _rows()
    assert [t["content"] for t in rows] == ["a", "b", "c", _VERIFY]

    output = await _tool().coroutine(
        action="add", content="d", after_todo_id=verify["todo_id"], session_id=_SID
    )
    assert "todowrite refused" in output
    assert "must remain last" in output


@pytest.mark.asyncio
async def test_taskflow_root_bind_keeps_verify_last():
    from agent.tools.taskflow.tools._shared import bind_flow_todo

    await _call(todos=[{"content": "a"}, {"content": "b"}])
    await bind_flow_todo(_SID, "flow-1", "flow root")

    rows = await _rows()
    assert rows[-1]["content"] == _VERIFY
    assert any(t["content"] == "flow root" for t in rows)


@pytest.mark.asyncio
async def test_shrinking_below_the_threshold_removes_the_verify():
    await _call(todos=[{"content": "a"}, {"content": "b"}])
    rows = await _rows()
    verify = _verify_row(rows)

    killed: list[str] = []

    async def _kill(todo: dict, session_id: str) -> None:
        killed.append(str(todo.get("subagent_id") or ""))

    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(todo_service, "_kill_verify_subagent", _kill)
    try:
        await _call(action="remove", todo_id=rows[0]["todo_id"])
    finally:
        monkeypatch.undo()

    rows = await _rows()
    assert [t["content"] for t in rows] == ["b"]
    assert killed == [""]
    assert verify["todo_id"] not in {t["todo_id"] for t in rows}


# ── G2/G3: a completed verify does not block a new plan; children never verify ─


@pytest.mark.asyncio
async def test_completed_verify_does_not_block_a_new_plan(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(todo_service, "_is_subagent_running", lambda _sid: False)
    await _call(todos=[{"content": "a"}, {"content": "b"}])
    rows = await _rows()
    verify = _verify_row(rows)
    for todo in rows:
        if todo_service.is_verify_todo(todo):
            continue
        await _call(action="patch", todo_id=todo["todo_id"], status="completed")
    await _call(action="patch", todo_id=verify["todo_id"], subagent_id="reviewer-1")
    await _call(action="patch", todo_id=verify["todo_id"], status="completed")

    # A new plan (active work) gets its own verify; the completed one stays.
    await _call(action="add", content="c")
    await _call(action="add", content="d")
    rows = await _rows()
    verifies = [t for t in rows if todo_service.is_verify_todo(t)]
    assert [t["status"] for t in verifies] == ["completed", "pending"]
    assert rows[-1] is verifies[-1] or rows[-1]["todo_id"] == verifies[-1]["todo_id"]


@pytest.mark.asyncio
async def test_a_subagent_session_never_gets_a_verify_todo():
    child = "agent:subagent:child-1"
    await todo_service.TodoService.update_todos(
        child, [{"content": "a", "status": "pending"}, {"content": "b", "status": "pending"}]
    )

    rows = await todo_store.get_todos(child)
    assert [t["content"] for t in rows] == ["a", "b"]
    assert not any(todo_service.is_verify_todo(t) for t in rows)


# ── the barrier ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_verify_completion_needs_a_finished_reviewer(monkeypatch: pytest.MonkeyPatch):
    running = {"value": True}
    monkeypatch.setattr(todo_service, "_is_subagent_running", lambda _sid: running["value"])
    await _call(todos=[{"content": "a"}, {"content": "b"}])
    rows = await _rows()
    verify = _verify_row(rows)
    for todo in rows:
        if not todo_service.is_verify_todo(todo):
            await _call(action="patch", todo_id=todo["todo_id"], status="completed")

    with pytest.raises(todo_service.TodoStoreError, match="no reviewer subagent is linked"):
        await _tool().coroutine(
            action="patch", todo_id=verify["todo_id"], status="completed", session_id=_SID
        )

    await _call(action="patch", todo_id=verify["todo_id"], subagent_id="reviewer-1")
    with pytest.raises(todo_service.TodoStoreError, match="still running"):
        await _tool().coroutine(
            action="patch", todo_id=verify["todo_id"], status="completed", session_id=_SID
        )

    running["value"] = False
    await _call(action="patch", todo_id=verify["todo_id"], status="completed")
    assert _verify_row(await _rows())["status"] == "completed"


@pytest.mark.asyncio
async def test_verify_cancellation_is_guarded(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(todo_service, "_is_subagent_running", lambda _sid: False)
    await _call(todos=[{"content": "a"}, {"content": "b"}])
    verify = _verify_row(await _rows())

    with pytest.raises(todo_service.TodoStoreError, match="cannot be cancelled directly"):
        await _tool().coroutine(
            action="patch", todo_id=verify["todo_id"], status="cancelled", session_id=_SID
        )

    # The barrier's second escape: every work todo cancelled (plan abandoned).
    rows = await _rows()
    abandoned = [
        {**todo, "status": "cancelled"} if not todo_service.is_verify_todo(todo) else todo
        for todo in rows
    ]
    await todo_service._assert_transition_allowed(
        verify, _SID, target_status="cancelled", todos=abandoned
    )


@pytest.mark.asyncio
async def test_the_exhausted_flag_allows_verify_cancellation(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(todo_service, "_is_subagent_running", lambda _sid: False)
    await _call(todos=[{"content": "a"}, {"content": "b"}])
    verify = _verify_row(await _rows())

    await todo_service.set_verify_budget_exhausted(_SID, True)
    assert await asyncio.to_thread(
        todo_service.state_register_db.get_state, _SID, "verify_budget_exhausted", False
    )
    await _call(action="patch", todo_id=verify["todo_id"], status="cancelled")

    row = _verify_row(await _rows())
    assert row["status"] == "cancelled"
    assert row["metadata"].get("cancelled_reason") == "budget_exhausted"
    assert not await asyncio.to_thread(
        todo_service.state_register_db.get_state, _SID, "verify_budget_exhausted", False
    )
    # The marker prevents normalization from appending a fresh verify on the
    # next mutation while the abandoned work is still pending.
    await _call(action="add", content="c")
    assert len([t for t in await _rows() if todo_service.is_verify_todo(t)]) == 1


@pytest.mark.asyncio
async def test_the_verify_todo_cannot_be_removed():
    await _call(todos=[{"content": "a"}, {"content": "b"}])
    verify = _verify_row(await _rows())

    with pytest.raises(todo_service.TodoStoreError, match="cannot be removed"):
        await _tool().coroutine(action="remove", todo_id=verify["todo_id"], session_id=_SID)
    assert _verify_row(await _rows())["todo_id"] == verify["todo_id"]


# ── failure rollback + notes ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_failure_rollback_reopens_only_the_named_todo_and_keeps_notes(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setattr(todo_service, "_is_subagent_running", lambda _sid: False)
    await _call(
        todos=[
            {"content": "a"},
            {"content": "b"},
            {"content": "c"},
        ]
    )
    rows = await _rows()
    work = {t["content"]: t for t in rows if not todo_service.is_verify_todo(t)}
    for todo in work.values():
        await _call(action="patch", todo_id=todo["todo_id"], status="completed")

    # A reviewer report names only "b" as failed.
    await _call(
        action="patch",
        todo_id=work["b"]["todo_id"],
        status="pending",
        notes="test_b failed: assert 1 == 2",
    )

    rows = await _rows()
    by_content = {t["content"]: t for t in rows}
    assert by_content["a"]["status"] == "completed"
    assert by_content["c"]["status"] == "completed"
    assert by_content["b"]["status"] == "pending"
    assert "assert 1 == 2" in by_content["b"]["notes"]

    from agent.middlewares.todo_continuation.core import _build_status_block

    block = _build_status_block(rows)
    assert "notes: test_b failed" in block
