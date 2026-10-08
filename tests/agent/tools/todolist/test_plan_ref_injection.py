"""Unit tests for the plan_ref session bridge (todowrite -> TodoService -> state).

A non-empty ``plan_ref`` on ``todowrite`` is forwarded to
``TodoService.update_todos`` and remembered once per session under the
``plan_ref`` state key; a call without one leaves the stored value untouched.
Persistence uses the shared ``isolated_db`` fixture, state is an in-memory
double, and the WS push seam returns "no websocket".
"""

import sys
from pathlib import Path

import pytest

from agent.tools.todolist import service as service_module
from agent.tools.todolist.service import TodoService
from agent.tools.todolist.tools import build_todolist_tools

pytestmark = [pytest.mark.unit]

todowrite_module = sys.modules["agent.tools.todolist.tools.todowrite"]


class FakeStateDB:
    """In-memory stand-in for state_register_db (get/set_state only)."""

    def __init__(self) -> None:
        self._states: dict[str, dict[str, object]] = {}

    def get_state(self, session_id: str, key: str, default: object = None) -> object:
        return self._states.get(session_id, {}).get(key, default)

    def set_state(self, session_id: str, key: str, value: object) -> bool:
        self._states.setdefault(session_id, {})[key] = value
        return True


class _NoWebsocket:
    """relation_register double: the fail-open push path finds no socket."""

    def get_websocket_by_session_id(self, session_id: str) -> None:
        return None


@pytest.fixture()
def fake_state(monkeypatch: pytest.MonkeyPatch) -> FakeStateDB:
    fake = FakeStateDB()
    monkeypatch.setattr("runtime.state_register_db", fake)
    return fake


def _todowrite():
    return {t.name: t for t in build_todolist_tools()}["todowrite"]


class TestTodoServicePlanRef:
    @pytest.mark.asyncio
    async def test_update_todos_writes_session_plan_ref(
        self,
        isolated_db: Path,
        fake_state: FakeStateDB,
        monkeypatch: pytest.MonkeyPatch,
    ):
        monkeypatch.setattr(service_module, "relation_register", _NoWebsocket())

        await TodoService.update_todos(
            "sess-1",
            [{"content": "a", "position": 0}],
            plan_ref="workspace/sessions/sess-1/plans/auth.md",
        )

        assert (
            fake_state.get_state("sess-1", "plan_ref") == "workspace/sessions/sess-1/plans/auth.md"
        )

    @pytest.mark.asyncio
    async def test_update_todos_writes_session_scoped_plan_ref(
        self,
        isolated_db: Path,
        fake_state: FakeStateDB,
        monkeypatch: pytest.MonkeyPatch,
    ):
        monkeypatch.setattr(service_module, "relation_register", _NoWebsocket())
        ref = "workspace/sessions/sess-new/plans/auth.md"

        await TodoService.update_todos("sess-new", [{"content": "a", "position": 0}], plan_ref=ref)

        assert fake_state.get_state("sess-new", "plan_ref") == ref

    @pytest.mark.asyncio
    async def test_update_todos_without_plan_ref_keeps_existing_value(
        self,
        isolated_db: Path,
        fake_state: FakeStateDB,
        monkeypatch: pytest.MonkeyPatch,
    ):
        fake_state.set_state("sess-1", "plan_ref", "workspace/sessions/sess-1/plans/old.md")
        monkeypatch.setattr(service_module, "relation_register", _NoWebsocket())

        await TodoService.update_todos("sess-1", [{"content": "a", "position": 0}])

        assert (
            fake_state.get_state("sess-1", "plan_ref") == "workspace/sessions/sess-1/plans/old.md"
        )

    @pytest.mark.asyncio
    async def test_update_todos_empty_plan_ref_keeps_existing_value(
        self,
        isolated_db: Path,
        fake_state: FakeStateDB,
        monkeypatch: pytest.MonkeyPatch,
    ):
        fake_state.set_state("sess-1", "plan_ref", "workspace/sessions/sess-1/plans/old.md")
        monkeypatch.setattr(service_module, "relation_register", _NoWebsocket())

        await TodoService.update_todos("sess-1", [{"content": "a", "position": 0}], plan_ref="")

        assert (
            fake_state.get_state("sess-1", "plan_ref") == "workspace/sessions/sess-1/plans/old.md"
        )


class TestTodowritePlanRef:
    @pytest.mark.asyncio
    async def test_todowrite_forwards_plan_ref_to_service(self, monkeypatch: pytest.MonkeyPatch):
        calls: list[tuple[str, list[dict], str | None]] = []

        async def _update(
            session_id: str, todos: list[dict], plan_ref: str | None = None
        ) -> list[dict]:
            calls.append((session_id, todos, plan_ref))
            return []

        monkeypatch.setattr(
            todowrite_module.service.TodoService, "update_todos", staticmethod(_update)
        )
        monkeypatch.setattr(todowrite_module, "_reminded_sessions", {"sess-1"})

        await _todowrite().coroutine(
            todos=[{"content": "x"}],
            plan_ref="workspace/sessions/sess-1/plans/auth.md",
            session_id="sess-1",
        )

        assert calls == [("sess-1", [{"content": "x"}], "workspace/sessions/sess-1/plans/auth.md")]

    @pytest.mark.asyncio
    async def test_todowrite_without_plan_ref_forwards_none(self, monkeypatch: pytest.MonkeyPatch):
        calls: list[tuple[str, list[dict], str | None]] = []

        async def _update(
            session_id: str, todos: list[dict], plan_ref: str | None = None
        ) -> list[dict]:
            calls.append((session_id, todos, plan_ref))
            return []

        monkeypatch.setattr(
            todowrite_module.service.TodoService, "update_todos", staticmethod(_update)
        )
        monkeypatch.setattr(todowrite_module, "_reminded_sessions", {"sess-2"})

        await _todowrite().coroutine(todos=[{"content": "x"}], session_id="sess-2")

        assert calls == [("sess-2", [{"content": "x"}], None)]

    @pytest.mark.asyncio
    async def test_todowrite_persists_plan_ref_through_real_service(
        self,
        isolated_db: Path,
        fake_state: FakeStateDB,
        monkeypatch: pytest.MonkeyPatch,
    ):
        monkeypatch.setattr(service_module, "relation_register", _NoWebsocket())
        monkeypatch.setattr(todowrite_module, "_reminded_sessions", {"sess-chain"})

        await _todowrite().coroutine(
            todos=[{"content": "chain", "position": 0}],
            plan_ref="workspace/sessions/sess-chain/plans/chain.md",
            session_id="sess-chain",
        )

        assert (
            fake_state.get_state("sess-chain", "plan_ref")
            == "workspace/sessions/sess-chain/plans/chain.md"
        )

    @pytest.mark.asyncio
    async def test_todowrite_without_plan_ref_leaves_state_untouched(
        self,
        isolated_db: Path,
        fake_state: FakeStateDB,
        monkeypatch: pytest.MonkeyPatch,
    ):
        fake_state.set_state("sess-keep", "plan_ref", "workspace/sessions/sess-keep/plans/keep.md")
        monkeypatch.setattr(service_module, "relation_register", _NoWebsocket())
        monkeypatch.setattr(todowrite_module, "_reminded_sessions", {"sess-keep"})

        await _todowrite().coroutine(
            todos=[{"content": "keep", "position": 0}], session_id="sess-keep"
        )

        assert (
            fake_state.get_state("sess-keep", "plan_ref")
            == "workspace/sessions/sess-keep/plans/keep.md"
        )
