"""E5 drain coverage: completion carriers get the Sisyphus verification reminder.

Extends ``tests/agent/tools/subagent/test_completion_drain.py`` without
depending on the subagent test stubs: the middleware's ``drain``/``rehydrate``
module references are monkeypatched with fakes, so the queue plumbing stays
untouched and only the reminder-append contract (plus fail-open behavior) is
exercised.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from langchain_core.messages import HumanMessage

from agent.middlewares import subagent_completion_drain as drain_mod
from agent.middlewares.subagent_completion_drain import (
    _VERIFICATION_REMINDER,
    SubagentCompletionDrainMiddleware,
)

pytestmark = [pytest.mark.unit]

SID = "sess-drain-reminder"


def _carrier(content: str) -> HumanMessage:
    """Task-4-shaped completion carrier (internal + provenance metadata)."""
    return HumanMessage(
        content=content,
        metadata={
            "internal": True,
            "provenance": "subagent_completion",
            "run_id": "run-1",
            "status": "completed",
        },
    )


@pytest.fixture()
def patched_drain(monkeypatch: pytest.MonkeyPatch) -> dict:
    """Replace the middleware's drain/rehydrate seams with an in-memory queue."""
    state: dict = {"items": [], "rehydrated": []}

    async def _rehydrate(key: str) -> None:
        state["rehydrated"].append(key)

    async def _drain(key: str) -> list:
        return list(state["items"])

    monkeypatch.setattr(drain_mod, "rehydrate", _rehydrate)
    monkeypatch.setattr(drain_mod, "drain", _drain)
    return state


def test_drained_carrier_content_ends_with_reminder(patched_drain: dict):
    patched_drain["items"] = [SimpleNamespace(message=_carrier("report ready"))]
    mw = SubagentCompletionDrainMiddleware()

    result = asyncio.run(mw.abefore_model({"session_id": SID}, None))

    assert result is not None
    message = result["messages"][0]
    assert message.text.startswith("report ready")
    assert message.text.endswith(_VERIFICATION_REMINDER)
    assert "SISYPHUS VIOLATION" in message.text
    assert message.metadata.get("provenance") == "subagent_completion"
    assert message.metadata.get("internal") is True


def test_non_carrier_message_is_left_unchanged(patched_drain: dict):
    patched_drain["items"] = [SimpleNamespace(message=HumanMessage(content="plain steer"))]
    mw = SubagentCompletionDrainMiddleware()

    result = asyncio.run(mw.abefore_model({"session_id": SID}, None))

    assert result is not None
    assert result["messages"][0].text == "plain steer"


def test_empty_queue_is_a_noop(patched_drain: dict):
    mw = SubagentCompletionDrainMiddleware()

    assert asyncio.run(mw.abefore_model({"session_id": SID}, None)) is None


def test_blank_session_id_is_a_noop(patched_drain: dict):
    patched_drain["items"] = [SimpleNamespace(message=_carrier("ignored"))]
    mw = SubagentCompletionDrainMiddleware()

    assert asyncio.run(mw.abefore_model({"session_id": "   "}, None)) is None
    assert patched_drain["rehydrated"] == []
