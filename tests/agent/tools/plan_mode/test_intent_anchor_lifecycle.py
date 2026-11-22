"""The approved-plan intent anchor lives and dies WITH its todo list.

One plan -> one anchor -> one todo list. When the list is cleared or fully
finished, the anchor must be dropped so its goal cannot leak into the next
task (the anchor rides the todo reminder into every turn). While any todo is
still active — including the system verify todo — the anchor stays, so the
``plan_drifted`` gate can still guard verify completion.
"""

from __future__ import annotations

import pytest

from agent.tools.todolist import service as service_mod
from agent.tools.todolist.service import _plan_is_gone, _sync_intent_anchor

pytestmark = [pytest.mark.unit]


class TestPlanIsGone:
    def test_empty_list_is_gone(self):
        assert _plan_is_gone([]) is True

    def test_all_completed_is_gone(self):
        todos = [
            {"status": "completed"},
            {"status": "completed"},
        ]
        assert _plan_is_gone(todos) is True

    def test_all_cancelled_is_gone(self):
        assert _plan_is_gone([{"status": "cancelled"}]) is True

    def test_mixed_terminal_is_gone(self):
        todos = [{"status": "completed"}, {"status": "cancelled"}]
        assert _plan_is_gone(todos) is True

    def test_any_pending_keeps_plan_alive(self):
        todos = [{"status": "completed"}, {"status": "pending"}]
        assert _plan_is_gone(todos) is False

    def test_in_progress_keeps_plan_alive(self):
        todos = [{"status": "completed"}, {"status": "in_progress"}]
        assert _plan_is_gone(todos) is False

    def test_verify_pending_keeps_plan_alive(self):
        """The verify todo is non-terminal; its pending state must retain the
        anchor so plan_drifted still guards completion."""
        todos = [
            {"status": "completed"},
            {"status": "pending", "content": "Verify the result"},
        ]
        assert _plan_is_gone(todos) is False

    def test_status_missing_counts_as_active(self):
        """A row with no status is treated as active (fail-safe: keep anchor)."""
        assert _plan_is_gone([{"content": "untagged"}]) is False


class TestSyncIntentAnchor:
    def _patch_clear(self, monkeypatch):
        calls: list[str] = []

        def fake_clear(session_id: str) -> None:
            calls.append(session_id)

        import agent.tools.plan_mode.intent_anchor as anchor_mod

        monkeypatch.setattr(anchor_mod, "clear_intent_anchor", fake_clear)
        return calls

    def test_gone_plan_clears_anchor(self, monkeypatch):
        calls = self._patch_clear(monkeypatch)
        _sync_intent_anchor("s1", [{"status": "completed"}])
        assert calls == ["s1"]

    def test_empty_list_clears_anchor(self, monkeypatch):
        calls = self._patch_clear(monkeypatch)
        _sync_intent_anchor("s1", [])
        assert calls == ["s1"]

    def test_live_plan_keeps_anchor(self, monkeypatch):
        calls = self._patch_clear(monkeypatch)
        _sync_intent_anchor("s1", [{"status": "pending"}])
        assert calls == []

    def test_empty_session_id_is_noop(self, monkeypatch):
        calls = self._patch_clear(monkeypatch)
        _sync_intent_anchor("", [])
        assert calls == []

    def test_clear_failure_never_raises(self, monkeypatch):
        import agent.tools.plan_mode.intent_anchor as anchor_mod

        def boom(session_id: str) -> None:
            raise RuntimeError("state down")

        monkeypatch.setattr(anchor_mod, "clear_intent_anchor", boom)
        _sync_intent_anchor("s1", [])  # must not raise


class TestTodoServiceIntegration:
    """End-to-end through the real TodoService: a todo list's lifecycle drives
    the anchor, not the plan-mode writer alone."""

    @pytest.mark.asyncio
    async def test_completing_last_todo_clears_anchor(self, monkeypatch, tmp_path):
        from agent.tools.todolist.registry import store_sqlite
        from agent.tools.todolist.service import TodoService
        from config.features import TODOLIST_INFRA

        monkeypatch.setitem(TODOLIST_INFRA, "verify_todo_enabled", False)
        monkeypatch.setattr(store_sqlite, "_DB_DIR", tmp_path)
        monkeypatch.setattr(store_sqlite, "_DB_PATH", tmp_path / "todos.db")
        monkeypatch.setattr(store_sqlite, "_initialized", False)
        monkeypatch.setattr(store_sqlite, "_init_loop", None)

        calls: list[str] = []

        def fake_clear(session_id: str) -> None:
            calls.append(session_id)

        import agent.tools.plan_mode.intent_anchor as anchor_mod

        monkeypatch.setattr(anchor_mod, "clear_intent_anchor", fake_clear)

        async def no_broadcast(*args, **kwargs):
            return None

        monkeypatch.setattr(service_mod, "broadcast_to_session", no_broadcast)

        async def no_sweep(*args, **kwargs):
            return None

        monkeypatch.setattr(service_mod, "_trigger_session_sweep", no_sweep)

        await TodoService.update_todos("s-int", [{"content": "do work", "status": "pending"}])
        assert calls == []  # live plan keeps the anchor

        todos = await TodoService.get_todos("s-int")
        await TodoService.patch_todo("s-int", int(todos[0]["todo_id"]), status="completed")
        assert calls == ["s-int"]  # plan finished -> anchor cleared

    @pytest.mark.asyncio
    async def test_clearing_list_clears_anchor(self, monkeypatch, tmp_path):
        from agent.tools.todolist.registry import store_sqlite
        from agent.tools.todolist.service import TodoService
        from config.features import TODOLIST_INFRA

        monkeypatch.setitem(TODOLIST_INFRA, "verify_todo_enabled", False)
        monkeypatch.setattr(store_sqlite, "_DB_DIR", tmp_path)
        monkeypatch.setattr(store_sqlite, "_DB_PATH", tmp_path / "todos.db")
        monkeypatch.setattr(store_sqlite, "_initialized", False)
        monkeypatch.setattr(store_sqlite, "_init_loop", None)

        calls: list[str] = []

        def fake_clear(session_id: str) -> None:
            calls.append(session_id)

        import agent.tools.plan_mode.intent_anchor as anchor_mod

        monkeypatch.setattr(anchor_mod, "clear_intent_anchor", fake_clear)

        async def no_broadcast(*args, **kwargs):
            return None

        monkeypatch.setattr(service_mod, "broadcast_to_session", no_broadcast)

        async def no_sweep(*args, **kwargs):
            return None

        monkeypatch.setattr(service_mod, "_trigger_session_sweep", no_sweep)

        await TodoService.update_todos("s-int2", [{"content": "do work", "status": "pending"}])
        assert calls == []

        await TodoService.update_todos("s-int2", [])
        assert calls == ["s-int2"]


class TestPushTodoUpdateHooksSync:
    @pytest.mark.asyncio
    async def test_push_clears_anchor_for_finished_plan(self, monkeypatch):
        calls: list[str] = []

        def fake_clear(session_id: str) -> None:
            calls.append(session_id)

        import agent.tools.plan_mode.intent_anchor as anchor_mod

        monkeypatch.setattr(anchor_mod, "clear_intent_anchor", fake_clear)

        async def no_broadcast(*args, **kwargs):
            return None

        monkeypatch.setattr(service_mod, "broadcast_to_session", no_broadcast)

        await service_mod._push_todo_update("s1", [{"status": "completed"}])
        assert calls == ["s1"]

    @pytest.mark.asyncio
    async def test_push_keeps_anchor_for_live_plan(self, monkeypatch):
        calls: list[str] = []

        def fake_clear(session_id: str) -> None:
            calls.append(session_id)

        import agent.tools.plan_mode.intent_anchor as anchor_mod

        monkeypatch.setattr(anchor_mod, "clear_intent_anchor", fake_clear)

        async def no_broadcast(*args, **kwargs):
            return None

        monkeypatch.setattr(service_mod, "broadcast_to_session", no_broadcast)

        await service_mod._push_todo_update("s1", [{"status": "in_progress"}])
        assert calls == []
