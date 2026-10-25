"""Model-retry progress: trajectory event + websocket frame (fail-open)."""

from __future__ import annotations

import asyncio

import pytest

from agent.middlewares.llm_retry import retry_events
from runtime import hooks

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]


async def test_publish_records_and_pushes_the_retry_frame(monkeypatch):
    recorded: list[tuple] = []
    pushed: list[tuple] = []

    hooks.register(hooks.RECORD_TRAJECTORY_EVENT, lambda *args: recorded.append(args))

    async def fake_broadcast(session_id, payload, **kwargs):
        pushed.append((session_id, payload))

    monkeypatch.setattr("runtime.session.ws_push.broadcast_to_session", fake_broadcast)

    retry_events.publish_model_retry(
        "s1",
        status="scheduled",
        attempt=2,
        max_attempts=11,
        retries=1,
        reason="rate_limit",
        delay_ms=2000,
    )
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    assert recorded == [
        (
            "s1",
            "model_retry",
            {
                "status": "scheduled",
                "attempt": 2,
                "budget_attempt": 2,
                "max_attempts": 11,
                "retries": 1,
                "extra_attempts": 0,
                "fallback_switches": 0,
                "reason": "rate_limit",
                "delay_ms": 2000,
            },
        )
    ]
    assert pushed and pushed[0][0] == "s1"
    assert pushed[0][1]["event"] == "model_retry"
    assert pushed[0][1]["content"]["attempt"] == 2
    hooks.unregister(hooks.RECORD_TRAJECTORY_EVENT)


async def test_publish_is_silent_without_a_loop_or_socket(monkeypatch):
    # No trajectory hook / no live socket: must not raise.
    hooks.unregister(hooks.RECORD_TRAJECTORY_EVENT)

    async def fake_broadcast(*args, **kwargs):
        raise RuntimeError("no socket")

    monkeypatch.setattr("runtime.session.ws_push.broadcast_to_session", fake_broadcast)
    retry_events.publish_model_retry(
        "s1", status="succeeded", attempt=2, max_attempts=11, retries=1
    )
    await asyncio.sleep(0)
    await asyncio.sleep(0)
