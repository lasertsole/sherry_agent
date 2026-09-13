"""Unit tests for TodoService: E6 validation, E4 transition barrier, WS push.

The service reaches three external seams (subagent registry liveness, TaskFlow
step status, WS push) plus the store. Tests substitute the seams through the
module-level injectable references on ``service`` and isolate persistence with
the shared ``isolated_db`` fixture, so no real TaskFlow/subagent/WS state is
touched.

Coverage: invalid category/delegation default-downgrade; valid fields and
status/priority preserved; update persists + one ``todo_updated`` WS frame;
completed todo blocked while its subagent run is live and nothing persisted;
completed todo with an ended/unknown run persists; completed todo blocked when
the linked TaskFlow step is not ``done``, missing, or its flow is gone, and
persists when the step is ``done``; a plain completed todo is never blocked;
no websocket and a failing websocket are both fail-open.
"""

import json
from pathlib import Path

import pytest

from agent.tools.todolist import service
from agent.tools.todolist.registry import store_sqlite
from agent.tools.todolist.service import TodoService, TodoStoreError

pytestmark = [pytest.mark.unit]


class _FakeWS:
    """Minimal websocket double: records send_text payloads or raises."""

    def __init__(self, sent: list[str], *, fail: bool = False) -> None:
        self._sent = sent
        self._fail = fail

    async def send_text(self, text: str) -> None:
        if self._fail:
            raise RuntimeError("socket closed")
        self._sent.append(text)


class _FakeRelationRegister:
    """Minimal relation_register double returning a preset websocket."""

    def __init__(self, ws: _FakeWS | None) -> None:
        self._ws = ws

    def get_websocket_by_session_id(self, session_id: str) -> _FakeWS | None:
        return self._ws


def _load_flow_returning(flow_id: str, step_id: str, status: str):
    """Build an async ``_load_flow`` seam returning one step with ``status``."""

    async def _load(requested_flow_id: str) -> dict:
        return {
            "flow_id": flow_id,
            "state": {"steps": [{"step_id": step_id, "status": status}]},
        }

    return _load


# --- E6 validation ---------------------------------------------------------


def test_validate_todos_downgrades_invalid_category_and_delegation():
    validated = service._validate_todos(
        [{"content": "bad", "category": "bogus", "delegation": "nope"}]
    )

    assert validated[0]["category"] == "quick"
    assert validated[0]["delegation"] == "self"


def test_validate_todos_preserves_valid_fields_and_status_priority():
    validated = service._validate_todos(
        [
            {
                "content": "ok",
                "category": "deep",
                "delegation": "subagent",
                "status": "in_progress",
                "priority": "high",
            }
        ]
    )

    assert validated[0]["category"] == "deep"
    assert validated[0]["delegation"] == "subagent"
    assert validated[0]["status"] == "in_progress"
    assert validated[0]["priority"] == "high"


def test_validate_todos_does_not_mutate_caller_dicts():
    original = {"content": "bad", "category": "bogus", "delegation": "nope"}

    validated = service._validate_todos([original])

    assert original == {"content": "bad", "category": "bogus", "delegation": "nope"}
    assert validated[0] is not original


# --- update_todos + WS push ------------------------------------------------


@pytest.mark.asyncio
async def test_update_todos_persists_and_pushes_one_todo_updated_frame(
    isolated_db: Path, monkeypatch: pytest.MonkeyPatch
):
    sent: list[str] = []
    monkeypatch.setattr(service, "relation_register", _FakeRelationRegister(_FakeWS(sent)))

    result = await TodoService.update_todos("sess-1", [{"content": "a", "position": 0}])

    rows = await store_sqlite.get_todos("sess-1")
    assert [t["content"] for t in rows] == ["a"]
    assert [t["content"] for t in result] == ["a"]
    assert len(sent) == 1
    payload = json.loads(sent[0])
    assert payload["event"] == "todo_updated"
    assert payload["session_id"] == "sess-1"
    assert [t["content"] for t in payload["content"]["todos"]] == ["a"]


@pytest.mark.asyncio
async def test_get_todos_reads_persisted_rows(
    isolated_db: Path,
):
    await store_sqlite.replace_all("sess-1", [{"content": "a", "position": 0}])

    assert [t["content"] for t in await TodoService.get_todos("sess-1")] == ["a"]
    assert await TodoService.get_todos("sess-missing") == []


# --- E4 barrier: subagent liveness -----------------------------------------


@pytest.mark.asyncio
async def test_completed_todo_with_live_subagent_is_blocked_and_nothing_persisted(
    isolated_db: Path, monkeypatch: pytest.MonkeyPatch
):
    await store_sqlite.replace_all("sess-1", [{"content": "old", "position": 0}])
    monkeypatch.setattr(service, "_get_run_by_child_session_key", lambda key: object())
    monkeypatch.setattr(service, "_is_live_unended_run", lambda run: True)

    with pytest.raises(TodoStoreError):
        await TodoService.update_todos(
            "sess-1",
            [
                {
                    "content": "new",
                    "position": 0,
                    "status": "completed",
                    "subagent_id": "agent:main:subagent:child-1",
                }
            ],
        )

    rows = await store_sqlite.get_todos("sess-1")
    assert [t["content"] for t in rows] == ["old"]


@pytest.mark.asyncio
async def test_completed_todo_with_ended_subagent_persists(
    isolated_db: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(service, "_get_run_by_child_session_key", lambda key: object())
    monkeypatch.setattr(service, "_is_live_unended_run", lambda run: False)

    result = await TodoService.update_todos(
        "sess-1",
        [{"content": "done", "status": "completed", "subagent_id": "child-1"}],
    )

    assert result[0]["status"] == "completed"


@pytest.mark.asyncio
async def test_completed_todo_with_unknown_subagent_record_persists(
    isolated_db: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(service, "_get_run_by_child_session_key", lambda key: None)

    result = await TodoService.update_todos(
        "sess-1",
        [{"content": "done", "status": "completed", "subagent_id": "missing"}],
    )

    assert result[0]["status"] == "completed"


# --- E4 barrier: TaskFlow step status --------------------------------------


@pytest.mark.asyncio
async def test_completed_todo_with_taskflow_step_not_done_is_blocked(
    isolated_db: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(
        service, "_load_flow", _load_flow_returning("flow-1", "step-1", "dispatched")
    )

    with pytest.raises(TodoStoreError):
        await TodoService.update_todos(
            "sess-1",
            [
                {
                    "content": "linked",
                    "status": "completed",
                    "flow_id": "flow-1",
                    "step_id": "step-1",
                }
            ],
        )

    assert await store_sqlite.get_todos("sess-1") == []


@pytest.mark.asyncio
async def test_completed_todo_with_taskflow_step_done_persists(
    isolated_db: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(service, "_load_flow", _load_flow_returning("flow-1", "step-1", "done"))

    result = await TodoService.update_todos(
        "sess-1",
        [
            {
                "content": "linked",
                "status": "completed",
                "flow_id": "flow-1",
                "step_id": "step-1",
            }
        ],
    )

    assert result[0]["status"] == "completed"


@pytest.mark.asyncio
async def test_completed_todo_with_missing_flow_is_blocked(
    isolated_db: Path, monkeypatch: pytest.MonkeyPatch
):
    async def _load_missing(flow_id: str) -> None:
        return None

    monkeypatch.setattr(service, "_load_flow", _load_missing)

    with pytest.raises(TodoStoreError) as excinfo:
        await TodoService.update_todos(
            "sess-1",
            [
                {
                    "content": "linked",
                    "status": "completed",
                    "flow_id": "flow-gone",
                    "step_id": "step-1",
                }
            ],
        )

    assert "flow-gone" in str(excinfo.value)
    assert await store_sqlite.get_todos("sess-1") == []


@pytest.mark.asyncio
async def test_completed_todo_with_missing_step_is_blocked(
    isolated_db: Path, monkeypatch: pytest.MonkeyPatch
):
    async def _load_other_step(flow_id: str) -> dict:
        return {
            "flow_id": flow_id,
            "state": {"steps": [{"step_id": "step-9", "status": "done"}]},
        }

    monkeypatch.setattr(service, "_load_flow", _load_other_step)

    with pytest.raises(TodoStoreError) as excinfo:
        await TodoService.update_todos(
            "sess-1",
            [
                {
                    "content": "linked",
                    "status": "completed",
                    "flow_id": "flow-1",
                    "step_id": "step-1",
                }
            ],
        )

    assert "step-1" in str(excinfo.value)


# --- E4 barrier: plain todos ------------------------------------------------


@pytest.mark.asyncio
async def test_plain_completed_todo_is_never_blocked(
    isolated_db: Path, monkeypatch: pytest.MonkeyPatch
):
    def _unexpected(*args: object, **kwargs: object) -> None:
        raise AssertionError("seam must not be consulted for a plain todo")

    monkeypatch.setattr(service, "_get_run_by_child_session_key", _unexpected)
    monkeypatch.setattr(service, "_load_flow", _unexpected)

    result = await TodoService.update_todos("sess-1", [{"content": "plain", "status": "completed"}])

    assert result[0]["status"] == "completed"


# --- WS push fail-open ------------------------------------------------------


@pytest.mark.asyncio
async def test_no_websocket_registered_does_not_raise(
    isolated_db: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(service, "relation_register", _FakeRelationRegister(None))

    result = await TodoService.update_todos("sess-1", [{"content": "a"}])

    assert result[0]["content"] == "a"


@pytest.mark.asyncio
async def test_ws_send_error_is_swallowed(isolated_db: Path, monkeypatch: pytest.MonkeyPatch):
    sent: list[str] = []
    monkeypatch.setattr(
        service,
        "relation_register",
        _FakeRelationRegister(_FakeWS(sent, fail=True)),
    )

    result = await TodoService.update_todos("sess-1", [{"content": "a"}])

    assert result[0]["content"] == "a"
    assert sent == []
