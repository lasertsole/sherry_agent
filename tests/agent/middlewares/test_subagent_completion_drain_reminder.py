"""Drain coverage: the always-on verification gate and carrier passthrough.

Extends ``tests/agent/tools/subagent/test_completion_drain.py`` without
depending on the subagent test stubs: the middleware's ``drain``/``rehydrate``
module references are monkeypatched with fakes, so the queue plumbing stays
untouched and only batch assembly (carriers kept verbatim + gate appended) and
fail-open behavior are exercised.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from langchain_core.messages import HumanMessage

from agent.middlewares.subagent_completion_drain import core as drain_mod
from agent.middlewares.subagent_completion_drain.core import (
    _VERIFICATION_GATE_MESSAGE,
    SubagentCompletionDrainMiddleware,
)
from agent.tools.subagent.announce import steering_queue as sq

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

    monkeypatch.setattr(sq, "rehydrate", _rehydrate)
    monkeypatch.setattr(sq, "drain", _drain)
    monkeypatch.setattr(drain_mod, "_backflow_shared_memory", lambda: None)
    monkeypatch.setattr(drain_mod, "_completion_gate_violated", lambda _key: True)
    return state


def test_every_carrier_is_injected_verbatim_and_gate_appended(patched_drain: dict):
    patched_drain["items"] = [
        SimpleNamespace(message=_carrier("report one")),
        SimpleNamespace(message=_carrier("report two")),
    ]
    mw = SubagentCompletionDrainMiddleware()

    result = asyncio.run(mw.abefore_model({"session_id": SID}, None))

    assert result is not None
    assert [m.text for m in result["messages"][:2]] == ["report one", "report two"]
    assert result["messages"][2].text == _VERIFICATION_GATE_MESSAGE
    assert result["messages"][0].metadata.get("provenance") == "subagent_completion"


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
