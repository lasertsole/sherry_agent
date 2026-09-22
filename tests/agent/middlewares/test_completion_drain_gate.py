"""Completion-drain programmatic gate tests.

The gate is unconditional: every drained subagent-completion batch is checked
against the session's verification evidence, and a mandatory-verification
message is appended when the session has no passing evidence. The evidence
collector is replaced only where the gate is concerned; the queue plumbing is
faked so no real steering queue is touched.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest
from langchain_core.messages import HumanMessage

from agent.middlewares.subagent_completion_drain import core as drain_mod
from agent.middlewares.subagent_completion_drain.core import (
    _VERIFICATION_GATE_MESSAGE,
    SubagentCompletionDrainMiddleware,
)
from agent.tools.subagent.announce import steering_queue as sq
from agent.tools.todolist.evidence_ledger import EvidenceLedger

pytestmark = [pytest.mark.unit]

SID = "sess-drain-gate"


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
    state: dict = {"items": []}

    async def _rehydrate(key: str) -> None:
        return None

    async def _drain(key: str) -> list:
        return list(state["items"])

    monkeypatch.setattr(sq, "rehydrate", _rehydrate)
    monkeypatch.setattr(sq, "drain", _drain)
    monkeypatch.setattr(drain_mod, "_backflow_shared_memory", lambda: None)
    return state


@pytest.fixture()
def ledger(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> EvidenceLedger:
    monkeypatch.setattr(EvidenceLedger, "LEDGER_PATH", str(tmp_path / "evidence.jsonl"))
    return EvidenceLedger


def _middleware() -> SubagentCompletionDrainMiddleware:
    return SubagentCompletionDrainMiddleware()


def test_passing_evidence_is_noop(patched_drain: dict, ledger: EvidenceLedger):
    patched_drain["items"] = [SimpleNamespace(message=_carrier("report ready"))]
    ledger.for_session(SID).append(kind="test", command="pytest -q", status="passed")

    result = asyncio.run(_middleware().abefore_model({"session_id": SID}, None))

    assert result is not None
    assert len(result["messages"]) == 1
    assert result["messages"][0].text == "report ready"


def test_missing_evidence_injects_gate(patched_drain: dict, ledger: EvidenceLedger):
    patched_drain["items"] = [SimpleNamespace(message=_carrier("report ready"))]

    result = asyncio.run(_middleware().abefore_model({"session_id": SID}, None))

    assert result is not None
    assert len(result["messages"]) == 2
    assert result["messages"][0].text == "report ready"
    gate = result["messages"][1]
    assert isinstance(gate, HumanMessage)
    assert gate.text == _VERIFICATION_GATE_MESSAGE


def test_failed_evidence_injects_gate(patched_drain: dict, ledger: EvidenceLedger):
    patched_drain["items"] = [SimpleNamespace(message=_carrier("report ready"))]
    ledger.for_session(SID).append(kind="test", command="pytest -q", status="failed")

    result = asyncio.run(_middleware().abefore_model({"session_id": SID}, None))

    assert result is not None
    assert len(result["messages"]) == 2
    assert result["messages"][1].text == _VERIFICATION_GATE_MESSAGE


def test_ignores_another_sessions_evidence(patched_drain: dict, ledger: EvidenceLedger):
    patched_drain["items"] = [SimpleNamespace(message=_carrier("report ready"))]
    ledger.for_session("another-session").append(kind="test", command="pytest -q", status="passed")

    result = asyncio.run(_middleware().abefore_model({"session_id": SID}, None))

    assert result is not None
    assert len(result["messages"]) == 2


def test_empty_queue_is_a_noop(patched_drain: dict, ledger: EvidenceLedger):
    assert asyncio.run(_middleware().abefore_model({"session_id": SID}, None)) is None


def test_fail_open_when_evidence_lookup_raises(
    patched_drain: dict, ledger: EvidenceLedger, monkeypatch: pytest.MonkeyPatch
):
    patched_drain["items"] = [SimpleNamespace(message=_carrier("report ready"))]
    from agent.tools.taskflow import evidence_collector

    def boom(**_kwargs):
        raise RuntimeError("ledger unavailable")

    monkeypatch.setattr(evidence_collector, "collect_evidence_summary", boom)

    result = asyncio.run(_middleware().abefore_model({"session_id": SID}, None))

    assert result is not None
    assert len(result["messages"]) == 1
    assert result["messages"][0].text == "report ready"


def test_sync_before_model_honours_the_gate(patched_drain: dict, ledger: EvidenceLedger):
    patched_drain["items"] = [SimpleNamespace(message=_carrier("report ready"))]

    result = _middleware().before_model({"session_id": SID}, None)

    assert result is not None
    assert len(result["messages"]) == 2
    assert result["messages"][1].text == _VERIFICATION_GATE_MESSAGE
