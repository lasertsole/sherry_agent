"""Trajectory compact events carry phase/reason (R6) on every compact path."""

from __future__ import annotations

import pytest
from langchain.agents.middleware import ModelRequest

from agent.middlewares.summarization import overflow as overflow_mod
from agent.middlewares.summarization.core import Summarization
from agent.middlewares.summarization.overflow import (
    ROUTE_COMPACT_ONLY,
    _ForcedRecoveryState,
)

pytestmark = [pytest.mark.unit]


@pytest.fixture()
def middleware() -> Summarization:
    return Summarization(model=None, main_llm_context_window=131_072)


@pytest.fixture()
def events(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, dict]]:
    captured: list[tuple[str, dict]] = []

    def _record(session_id: str, kind: str, payload: dict) -> None:
        captured.append((kind, payload))

    monkeypatch.setattr(overflow_mod, "record_trajectory_event", _record)
    return captured


def _request() -> ModelRequest:
    return ModelRequest(model=None, messages=[], state={"session_id": "traj-sess"})


@pytest.mark.parametrize(
    ("trigger", "phase", "reason"),
    [
        ("T1", "pre_request", "context_limit"),
        ("T2", "pre_request", "context_limit"),
        ("T3", "post_response", "context_limit"),
        ("USER", "standalone_turn", "user_requested"),
    ],
)
def test_finish_compact_records_phase_reason_and_keeps_legacy_fields(
    middleware, events, trigger, phase, reason
):
    middleware._finish_compact(
        _request(),
        ROUTE_COMPACT_ONLY,
        "traj-sess",
        trigger,
        old_tokens=100,
        usable=1_000,
        messages_before=10,
    )

    assert len(events) == 1
    kind, payload = events[0]
    assert kind == "compact"
    assert payload["phase"] == phase
    assert payload["reason"] == reason
    assert payload["trigger"] == trigger
    assert payload["messages_before"] == 10
    assert payload["messages_after"] == 0
    assert payload["route"] == ROUTE_COMPACT_ONLY


@pytest.mark.parametrize(("trigger", "phase"), [("T4", "reactive"), ("T5", "reactive")])
def test_forced_recovery_records_reactive_provider_overflow(middleware, events, trigger, phase):
    state = _ForcedRecoveryState(
        trigger=trigger,
        retry_key="summarization_overflow_retries",
        attempt=1,
        old_tokens=100,
        messages_before=7,
    )
    middleware._finish_forced_recovery(_request(), "traj-sess", "context_overflow", state)

    assert len(events) == 1
    kind, payload = events[0]
    assert kind == "compact"
    assert payload["phase"] == phase
    assert payload["reason"] == "provider_overflow"
    assert payload["trigger"] == trigger
    assert payload["messages_before"] == 7
    assert payload["route"] == "reactive_recovery"
