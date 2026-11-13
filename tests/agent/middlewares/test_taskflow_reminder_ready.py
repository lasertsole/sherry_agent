"""The TaskFlow board's ready-set reminder and its live cooldown.

The board names every ready step (not only ``pending[0]``), and its cooldown
shortens while ready work awaits dispatch. These tests pin the readiness filter,
the conditional cooldown and the catalogue's callable-cooldown support.
"""

from __future__ import annotations

import pytest

from agent.middlewares.reminder_catalog import sources
from agent.middlewares.reminder_catalog.core import (
    ReminderChannel,
    ReminderSource,
    clear_session,
    resolve_injections,
)
from config.features import TASKFLOW_INFRA
from workspace.prompt_builder import _taskflow_ready_steps, has_undispatched_ready_steps

pytestmark = [pytest.mark.unit]


def _step(
    step_id: str,
    status: str,
    depends_on: list[str] | None = None,
    child: str | None = None,
) -> dict:
    step = {
        "step_id": step_id,
        "task": f"task-{step_id}",
        "status": status,
        "depends_on": depends_on or [],
    }
    if child is not None:
        step["child_session_key"] = child
    return step


def test_ready_steps_exclude_unsatisfied_and_in_flight_work():
    statuses = {
        "done": "done",
        "ready": "ready",
        "blocked": "blocked",
        "dispatched": "dispatched",
    }
    steps = [
        _step("done", "done"),
        _step("ready", "ready", depends_on=["done"]),
        _step("blocked", "blocked", depends_on=["dispatched"]),
        _step("dispatched", "dispatched"),
        # A hand-edited ready step whose dependency is not done is not safe to
        # advertise as parallelizable.
        _step("stale", "ready", depends_on=["blocked"]),
        # A ready status that already carries a child key is inconsistent (it is
        # in flight); it must not be advertised as a new dispatch opportunity.
        _step("in-flight-ready", "ready", child="agent:main:subagent:x"),
    ]

    ready = _taskflow_ready_steps(steps, lambda step: statuses.get(step["step_id"], "ready"))

    assert [step["step_id"] for step in ready] == ["ready"]


def test_has_undispatched_ready_steps_is_session_scoped(monkeypatch):
    import workspace.prompt_builder as prompt_builder

    class _Provider:
        @staticmethod
        def step_status(step):
            return step["status"]

        @staticmethod
        def get_active_flows(session_id):
            if session_id != "me":
                return []
            return [{"state": {"steps": [_step("s1", "ready")]}}]

    monkeypatch.setattr(prompt_builder, "_resolve_provider", lambda _name: _Provider())

    assert has_undispatched_ready_steps("me") is True
    assert has_undispatched_ready_steps("someone-else") is False


def _patch_attention_flows(monkeypatch, steps):
    import workspace.prompt_builder as prompt_builder

    class _Provider:
        @staticmethod
        def step_status(step):
            return step["status"]

        @staticmethod
        def get_active_flows(_session_id):
            return [{"state": {"steps": steps}}]

    monkeypatch.setattr(prompt_builder, "_resolve_provider", lambda _name: _Provider())


def test_attention_reads_auto_pending_when_only_ready_work_waits(monkeypatch):
    from workspace.prompt_builder import taskflow_attention

    _patch_attention_flows(monkeypatch, [_step("s1", "ready")])

    assert taskflow_attention("me") == "auto_pending"


def test_attention_reads_failed_spawn_from_the_persisted_marker(monkeypatch):
    from workspace.prompt_builder import taskflow_attention

    step = _step("s1", "ready")
    step["last_spawn_error"] = "RuntimeError: lane refused"
    _patch_attention_flows(monkeypatch, [step])

    assert taskflow_attention("me") == "failed_spawn"


def test_attention_ignores_a_step_merely_waiting_on_its_upstream(monkeypatch):
    from workspace.prompt_builder import taskflow_attention

    _patch_attention_flows(
        monkeypatch,
        [_step("s1", "dispatched"), _step("s2", "blocked", depends_on=["s1"])],
    )

    assert taskflow_attention("me") == "none"


def test_attention_reads_a_blocked_decision(monkeypatch):
    from workspace.prompt_builder import taskflow_attention

    step = _step("s1", "blocked")
    step["block_reason"] = "StepJudge blocked: no evidence"
    _patch_attention_flows(monkeypatch, [step])

    assert taskflow_attention("me") == "blocked"


def test_board_cooldown_shortens_for_a_failed_spawn(monkeypatch):
    monkeypatch.setattr(sources, "taskflow_attention", lambda _sid: "failed_spawn")
    monkeypatch.setitem(TASKFLOW_INFRA, "taskflow_reminder_cooldown_when_ready", 1)

    assert sources._taskflow_cooldown("sid", {}) == 1


def test_board_cooldown_shortens_for_a_blocked_step(monkeypatch):
    monkeypatch.setattr(sources, "taskflow_attention", lambda _sid: "blocked")
    monkeypatch.setitem(TASKFLOW_INFRA, "taskflow_reminder_cooldown_when_ready", 1)

    assert sources._taskflow_cooldown("sid", {}) == 1


def test_board_cooldown_stays_long_while_auto_dispatch_covers_ready_work(monkeypatch):
    monkeypatch.setattr(sources, "taskflow_attention", lambda _sid: "auto_pending")
    monkeypatch.setattr(sources, "auto_dispatch_waves_enabled", lambda: True)

    assert sources._taskflow_cooldown("sid", {}) == 5


def test_board_cooldown_shortens_for_ready_work_when_a_wave_is_off(monkeypatch):
    monkeypatch.setattr(sources, "taskflow_attention", lambda _sid: "auto_pending")
    monkeypatch.setattr(sources, "auto_dispatch_waves_enabled", lambda: False)
    monkeypatch.setitem(TASKFLOW_INFRA, "taskflow_reminder_cooldown_when_ready", 1)

    assert sources._taskflow_cooldown("sid", {}) == 1


def test_board_cooldown_returns_to_five_with_nothing_outstanding(monkeypatch):
    monkeypatch.setattr(sources, "taskflow_attention", lambda _sid: "none")

    assert sources._taskflow_cooldown("sid", {}) == 5


def test_a_callable_cooldown_is_evaluated_per_turn(monkeypatch):
    import agent.middlewares.reminder_catalog.core as core_mod

    source = ReminderSource(
        source_id="dynamic",
        channel=ReminderChannel.CURRENT_TURN,
        builder=lambda _sid, state: state["text"],
        cooldown_turns=lambda _sid, state: state["cooldown"],
    )
    monkeypatch.setattr(core_mod, "_SOURCES", {"dynamic": source})
    monkeypatch.setattr(core_mod, "_THROTTLE_STATE", {})
    try:
        assert resolve_injections("s", {"text": "A", "cooldown": 5}, current_turn=1) == {
            "dynamic": "A"
        }
        for turn in range(2, 6):
            assert (
                resolve_injections("s", {"text": f"B{turn}", "cooldown": 5}, current_turn=turn)
                == {}
            )
        assert resolve_injections("s", {"text": "F", "cooldown": 5}, current_turn=6) == {
            "dynamic": "F"
        }
        # The next turn's resolver now says 1: a changed board is eligible again.
        assert resolve_injections("s", {"text": "G", "cooldown": 1}, current_turn=7) == {
            "dynamic": "G"
        }
    finally:
        clear_session("s")


def test_a_broken_cooldown_resolver_does_not_suppress_the_source(monkeypatch):
    import agent.middlewares.reminder_catalog.core as core_mod

    def _boom(_sid, _state):
        raise RuntimeError("bad resolver")

    source = ReminderSource(
        source_id="broken-cooldown",
        channel=ReminderChannel.CURRENT_TURN,
        builder=lambda _sid, _state: "X",
        cooldown_turns=_boom,
    )
    monkeypatch.setattr(core_mod, "_SOURCES", {"broken-cooldown": source})
    monkeypatch.setattr(core_mod, "_THROTTLE_STATE", {})
    try:
        assert resolve_injections("s", {}, current_turn=1) == {"broken-cooldown": "X"}
        assert resolve_injections("s", {}, current_turn=2) == {"broken-cooldown": "X"}
    finally:
        clear_session("s")
