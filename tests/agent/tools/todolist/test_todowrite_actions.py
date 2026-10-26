"""Todo local actions: stable todo_id addressing, insertion, removal, reattach."""

import json
import sys

import pytest

from agent.tools.todolist import service as todo_service
from agent.tools.todolist.registry import store_sqlite as todo_store
from agent.tools.todolist.tools import _FANOUT_REMINDER, build_todolist_tools

pytestmark = [pytest.mark.unit]

_SESSION = "sess-todo-actions"
_TODOWRITE_MODULE = sys.modules["agent.tools.todolist.tools.todowrite"]


async def _no_broadcast(*_args, **_kwargs) -> int:
    return 0


def _tool():
    return {t.name: t for t in build_todolist_tools()}["todowrite"]


def _parse(output: str):
    return json.loads(output.replace(_FANOUT_REMINDER, ""))


@pytest.fixture(autouse=True)
def _isolate(monkeypatch: pytest.MonkeyPatch, isolated_db):
    monkeypatch.setattr(_TODOWRITE_MODULE, "_reminded_sessions", set())
    monkeypatch.setattr(todo_service, "broadcast_to_session", _no_broadcast)


@pytest.mark.asyncio
async def test_replace_returns_todo_ids():
    output = await _tool().coroutine(
        todos=[{"content": "a"}, {"content": "b"}], session_id=_SESSION
    )

    rows = _parse(output)
    assert [row["todo_id"] for row in rows] == [1, 2]


@pytest.mark.asyncio
async def test_todoread_returns_todo_ids():
    await _tool().coroutine(todos=[{"content": "a"}], session_id=_SESSION)
    tools = {t.name: t for t in build_todolist_tools()}

    output = await tools["todoread"].coroutine(session_id=_SESSION)

    rows = json.loads(output)
    assert [row["todo_id"] for row in rows] == [1]


@pytest.mark.asyncio
async def test_patch_changes_content_without_touching_flow_or_position():
    seeded = _parse(
        await _tool().coroutine(
            todos=[
                {"content": "a", "flow_id": "flow-1", "step_id": "step-1"},
                {"content": "b"},
            ],
            session_id=_SESSION,
        )
    )
    target = seeded[0]

    output = await _tool().coroutine(
        action="patch",
        todo_id=target["todo_id"],
        content="a-updated",
        session_id=_SESSION,
    )

    updated = _parse(output)
    assert updated["content"] == "a-updated"
    assert updated["position"] == target["position"]
    assert updated["flow_id"] == "flow-1"
    assert updated["step_id"] == "step-1"
    rows = await todo_store.get_todos(_SESSION)
    assert [row["content"] for row in rows] == ["a-updated", "b"]


@pytest.mark.asyncio
async def test_add_after_todo_id_inserts_and_preserves_existing_ids():
    seeded = _parse(
        await _tool().coroutine(
            todos=[{"content": "a"}, {"content": "b"}, {"content": "c"}],
            session_id=_SESSION,
        )
    )

    output = await _tool().coroutine(
        action="add",
        content="inserted",
        after_todo_id=seeded[0]["todo_id"],
        session_id=_SESSION,
    )

    rows = _parse(output)
    assert [row["content"] for row in rows] == ["a", "inserted", "b", "c"]
    assert [row["todo_id"] for row in rows] == [1, 4, 2, 3]


@pytest.mark.asyncio
async def test_missing_local_todo_id_returns_the_valid_id_list():
    seeded = _parse(
        await _tool().coroutine(todos=[{"content": "a"}, {"content": "b"}], session_id=_SESSION)
    )

    patch_output = await _tool().coroutine(
        action="patch", todo_id=99, content="nope", session_id=_SESSION
    )
    add_output = await _tool().coroutine(
        action="add", content="nope", after_todo_id=99, session_id=_SESSION
    )

    for output in (patch_output, add_output):
        assert "not found" in output
        assert "#1" in output and "#2" in output
    assert seeded[0]["todo_id"] == 1


@pytest.mark.asyncio
async def test_remove_requires_todo_id_and_deletes_only_that_row():
    seeded = _parse(
        await _tool().coroutine(
            todos=[{"content": "a"}, {"content": "b"}, {"content": "c"}],
            session_id=_SESSION,
        )
    )

    missing = await _tool().coroutine(action="remove", session_id=_SESSION)
    assert "requires todo_id" in missing

    removed = _parse(
        await _tool().coroutine(action="remove", todo_id=seeded[1]["todo_id"], session_id=_SESSION)
    )
    assert removed["content"] == "b"
    assert [row["content"] for row in await todo_store.get_todos(_SESSION)] == ["a", "c"]


@pytest.mark.asyncio
async def test_remove_flow_root_triggers_immediate_compensation(monkeypatch):
    seeded = _parse(
        await _tool().coroutine(
            todos=[{"content": "root", "flow_id": "flow-1"}], session_id=_SESSION
        )
    )
    calls: list[tuple[str, str]] = []

    async def _compensate(session_id: str, flow_id: str) -> None:
        calls.append((session_id, flow_id))

    monkeypatch.setattr(todo_service, "_compensate_removed_flow", _compensate)

    await _tool().coroutine(action="remove", todo_id=seeded[0]["todo_id"], session_id=_SESSION)

    assert calls == [(_SESSION, "flow-1")]


@pytest.mark.asyncio
async def test_compensation_fails_a_flow_only_when_no_root_remains(monkeypatch):
    calls: list[str] = []

    async def _get_flow(flow_id: str, session_id: str):
        return {"flow_id": flow_id, "status": "running", "state": {}, "expected_revision": 1}

    async def _fail_flow(flow: dict, *, reason: str) -> bool:
        calls.append(reason)
        return True

    async def _no_roots(_session_id: str) -> set[str]:
        return set()

    async def _rooted(_session_id: str) -> set[str]:
        return {"flow-1"}

    monkeypatch.setattr("agent.tools.taskflow.registry.store_sqlite.get_flow", _get_flow)
    monkeypatch.setattr("agent.tools.taskflow.registry.store_sqlite.fail_flow_for_gc", _fail_flow)
    monkeypatch.setattr(todo_store, "get_active_flow_ids", _no_roots)

    await todo_service._compensate_removed_flow(_SESSION, "flow-1")
    assert calls == ["todo_removed_explicitly"]

    calls.clear()
    monkeypatch.setattr(todo_store, "get_active_flow_ids", _rooted)
    await todo_service._compensate_removed_flow(_SESSION, "flow-1")
    assert calls == []


@pytest.mark.asyncio
async def test_reattach_binds_an_existing_todo_to_an_active_flow(monkeypatch):
    seeded = _parse(await _tool().coroutine(todos=[{"content": "root"}], session_id=_SESSION))

    async def _active_flow(flow_id: str, session_id: str):
        return {"flow_id": flow_id, "status": "running", "state": {}}

    monkeypatch.setattr(todo_service, "_load_flow", _active_flow)

    output = await _tool().coroutine(
        action="reattach",
        todo_id=seeded[0]["todo_id"],
        flow_id="flow-orphan",
        session_id=_SESSION,
    )

    rebound = _parse(output)
    assert rebound["todo_id"] == seeded[0]["todo_id"]
    assert rebound["flow_id"] == "flow-orphan"


@pytest.mark.asyncio
async def test_reattach_refuses_a_terminal_flow(monkeypatch):
    seeded = _parse(await _tool().coroutine(todos=[{"content": "root"}], session_id=_SESSION))

    async def _terminal_flow(flow_id: str, session_id: str):
        return {"flow_id": flow_id, "status": "done", "state": {}}

    monkeypatch.setattr(todo_service, "_load_flow", _terminal_flow)

    output = await _tool().coroutine(
        action="reattach",
        todo_id=seeded[0]["todo_id"],
        flow_id="flow-done",
        session_id=_SESSION,
    )

    assert "todowrite refused" in output
    assert "terminal" in output


@pytest.mark.asyncio
async def test_list_orphans_excludes_flows_with_an_active_root(monkeypatch):
    await _tool().coroutine(
        todos=[{"content": "root", "flow_id": "flow-rooted"}], session_id=_SESSION
    )

    async def _flows(_session_id: str):
        return [
            {
                "flow_id": "flow-rooted",
                "status": "running",
                "state": {"description": "rooted"},
                "child_session_key": None,
                "expected_revision": 1,
            },
            {
                "flow_id": "flow-orphan",
                "status": "running",
                "state": {"description": "orphan"},
                "child_session_key": "child-1",
                "expected_revision": 2,
            },
        ]

    monkeypatch.setattr(
        "agent.tools.taskflow.registry.store_sqlite.get_non_terminal_flows",
        _flows,
    )

    output = await _tool().coroutine(action="list_orphans", session_id=_SESSION)

    orphans = _parse(output)
    assert [flow["flow_id"] for flow in orphans] == ["flow-orphan"]
