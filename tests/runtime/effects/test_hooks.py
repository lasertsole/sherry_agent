"""The three feature-flagged call sites: execution, shadow, and off.

The flags are read from ``runtime.effects.config`` inside each branch, so these
tests monkeypatch the config module (not the caller's imported name) and assert
the original path is untouched when both flags are off.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest
from langchain_core.messages import HumanMessage

from agent.tools.todolist import stagnation_tracker as st

import agent.middlewares.todo_continuation.core as cont_core
import agent.tools.subagent.announce.delivery as delivery
import runtime.effects.config as effects_config
import runtime.effects.intent_log as intent_log
import server.service.auto_turn as auto_turn
from server.service.effects.handlers import (
    handle_auto_turn,
    handle_completion_notice,
    handle_continuation,
)

pytestmark = [pytest.mark.unit]


def _flags(monkeypatch: pytest.MonkeyPatch, *, execution: bool, shadow: bool) -> None:
    monkeypatch.setattr(effects_config, "effects_execution_enabled", lambda: execution)
    monkeypatch.setattr(effects_config, "effects_shadow_enabled", lambda: shadow)


class TestContinuationInjection:
    def _mw(self):
        return cont_core.PlanContinuationEnforcer()

    def _trigger(self, result):
        async def trigger(session_key, injection):
            trigger.calls.append((session_key, injection))
            return result

        trigger.calls = []
        return trigger

    @pytest.mark.asyncio
    async def test_execution_enqueues_and_marks_the_turn(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        mw = self._mw()
        trigger = self._trigger(SimpleNamespace(outcome="triggered"))
        monkeypatch.setattr(cont_core, "_resolve_hook", lambda name: trigger)
        _flags(monkeypatch, execution=True, shadow=False)
        enqueued: list[dict] = []
        monkeypatch.setattr(
            intent_log, "enqueue", lambda **kwargs: enqueued.append(kwargs) or (1, 1)
        )
        session_id = f"s-{uuid.uuid4().hex[:8]}"

        assert await mw._inject(session_id, "continue the plan") is True

        assert trigger.calls == []
        assert enqueued and enqueued[0]["kind"] == "continuation"
        assert st.continuation_turns(session_id) == 1

    @pytest.mark.asyncio
    async def test_shadow_runs_the_original_and_marks_done(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        mw = self._mw()
        trigger = self._trigger(SimpleNamespace(outcome="triggered"))
        monkeypatch.setattr(cont_core, "_resolve_hook", lambda name: trigger)
        _flags(monkeypatch, execution=False, shadow=True)
        enqueued: list[dict] = []
        done: list[tuple] = []
        monkeypatch.setattr(
            intent_log, "enqueue", lambda **kwargs: enqueued.append(kwargs) or (7, 3)
        )
        monkeypatch.setattr(intent_log, "mark_done", lambda *args: done.append(args))
        session_id = f"s-{uuid.uuid4().hex[:8]}"

        assert await mw._inject(session_id, "continue the plan") is True

        assert len(trigger.calls) == 1
        assert enqueued and enqueued[0]["kind"] == "continuation"
        assert done == [(7, 3)]

    @pytest.mark.asyncio
    async def test_flags_off_keeps_the_original_path(self, monkeypatch: pytest.MonkeyPatch) -> None:
        mw = self._mw()
        trigger = self._trigger(SimpleNamespace(outcome="triggered"))
        monkeypatch.setattr(cont_core, "_resolve_hook", lambda name: trigger)
        _flags(monkeypatch, execution=False, shadow=False)
        monkeypatch.setattr(
            intent_log,
            "enqueue",
            lambda **kwargs: (_ for _ in ()).throw(AssertionError("enqueued")),
        )
        session_id = f"s-{uuid.uuid4().hex[:8]}"

        assert await mw._inject(session_id, "continue the plan") is True
        assert len(trigger.calls) == 1


class TestAutoTurn:
    @pytest.mark.asyncio
    async def test_execution_enqueues_without_spawning(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _flags(monkeypatch, execution=True, shadow=False)
        enqueued: list[dict] = []
        monkeypatch.setattr(
            intent_log, "enqueue", lambda **kwargs: enqueued.append(kwargs) or (1, 1)
        )

        async def fail_impl(*args, **kwargs):
            raise AssertionError("the implementation must not run in execution mode")

        monkeypatch.setattr(auto_turn, "_maybe_trigger_auto_turn_impl", fail_impl)

        result = await auto_turn.maybe_trigger_auto_turn(
            "agent:main:session:s1", HumanMessage(content="finish")
        )

        assert result.outcome == auto_turn.AutoTurnOutcome.TRIGGERED
        assert result.reason == "effects_queue"
        assert enqueued and enqueued[0]["kind"] == "auto_turn"

    @pytest.mark.asyncio
    async def test_flags_off_keeps_the_impl(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _flags(monkeypatch, execution=False, shadow=False)
        called = {"n": 0}

        async def fake_impl(bare, injection):
            called["n"] += 1
            return auto_turn.AutoTurnResult(auto_turn.AutoTurnOutcome.TRIGGERED, bare)

        monkeypatch.setattr(auto_turn, "_maybe_trigger_auto_turn_impl", fake_impl)

        result = await auto_turn.maybe_trigger_auto_turn(
            "agent:main:session:s2", HumanMessage(content="finish")
        )

        assert result.outcome == auto_turn.AutoTurnOutcome.TRIGGERED
        assert called["n"] == 1


class TestCompletionDelivery:
    @pytest.mark.asyncio
    async def test_execution_enqueues_and_answers_accepted(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _flags(monkeypatch, execution=True, shadow=False)
        enqueued: list[dict] = []
        monkeypatch.setattr(
            intent_log, "enqueue", lambda **kwargs: enqueued.append(kwargs) or (1, 1)
        )
        run = SimpleNamespace(
            run_id="run-1",
            requester_session_key="session-1",
            completion=SimpleNamespace(result_text="the result"),
        )

        result = await delivery.deliver_subagent_announcement(run)

        assert result.success is True
        assert enqueued and enqueued[0]["kind"] == "completion_notice"
        assert enqueued[0]["key"] == "run:run-1"

    @pytest.mark.asyncio
    async def test_shadow_runs_the_impl_and_marks_done(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _flags(monkeypatch, execution=False, shadow=True)
        enqueued: list[dict] = []
        done: list[tuple] = []
        monkeypatch.setattr(
            intent_log, "enqueue", lambda **kwargs: enqueued.append(kwargs) or (4, 2)
        )
        monkeypatch.setattr(intent_log, "mark_done", lambda *args: done.append(args))
        called = {"n": 0}

        async def fake_impl(run):
            called["n"] += 1
            return delivery.AnnounceDeliveryResult(success=True)

        monkeypatch.setattr(delivery, "_deliver_subagent_announcement_impl", fake_impl)
        run = SimpleNamespace(
            run_id="run-2",
            requester_session_key="session-2",
            completion=SimpleNamespace(result_text="done"),
        )

        result = await delivery.deliver_subagent_announcement(run)

        assert result.success is True
        assert called["n"] == 1
        assert enqueued and enqueued[0]["key"] == "run:run-2"
        assert done == [(4, 2)]


class TestHandlers:
    @pytest.mark.asyncio
    async def test_continuation_handler_done_and_refused(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import runtime.hooks as hooks

        async def trigger(session_key, injection):
            return SimpleNamespace(outcome=SimpleNamespace(value="triggered"))

        monkeypatch.setattr(hooks, "resolve", lambda name: trigger)
        assert (await handle_continuation({"session_key": "s", "prompt": "p"})).done is True

        async def busy(session_key, injection):
            return SimpleNamespace(outcome=SimpleNamespace(value="busy"))

        monkeypatch.setattr(hooks, "resolve", lambda name: busy)
        result = await handle_continuation({"session_key": "s", "prompt": "p"})
        assert result.done is False and result.retry_after_s is not None

    @pytest.mark.asyncio
    async def test_completion_handler_abandons_a_purged_run(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import agent.tools.subagent.registry.read as read_mod

        monkeypatch.setattr(read_mod, "get_run_readonly", lambda run_id: None)

        result = await handle_completion_notice({"run_id": "gone"})
        assert result.abandon is True

    @pytest.mark.asyncio
    async def test_auto_turn_handler_retries_busy(self, monkeypatch: pytest.MonkeyPatch) -> None:
        async def fake_trigger(session_key, injection, **kwargs):
            return SimpleNamespace(outcome=auto_turn.AutoTurnOutcome.BUSY)

        monkeypatch.setattr(auto_turn, "maybe_trigger_auto_turn", fake_trigger)

        result = await handle_auto_turn({"session_key": "s", "content": "go"})
        assert result.done is False and result.retry_after_s is not None
