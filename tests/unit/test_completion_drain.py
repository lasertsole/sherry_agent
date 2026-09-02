"""Task 7 — before_model middleware: subagent completion drain (TDD).

Contractual test names (task 7):
- test_drain_before_model_call
- test_internal_message_skips_guard_and_budget
- test_resume_drains_persisted_injection
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage, HumanMessage

from agent.middlewares.iteration_budget import IterationBudget
from agent.middlewares.output_repetition_guard import (
    SESSION_STATE_KEYS,
    OutputRepetitionGuard,
    _HISTORY_KEY,
    _REASONING_HISTORY_KEY,
)
from agent.middlewares.subagent_completion_drain import (
    SubagentCompletionDrainMiddleware,
)
from agent.tools.subagent.announce import steering_queue as sq
from agent.tools.subagent.announce.steering_queue import SteeringQueue
from agent.tools.subagent.registry.pending_injections import PendingInjectionStore
from runtime import state_register_mem

pytestmark = pytest.mark.unit

SID_DRAIN = "sess-cd-drain"
SID_RESUME = "sess-cd-resume"
SID_GUARD = "sess-cd-guard"


def _completion_human(
    content: str = "子任务完成: report ready",
    run_id: str = "run-test-1",
) -> HumanMessage:
    """Task-4-shaped completion carrier (frozen metadata contract)."""
    return HumanMessage(
        content=content,
        metadata={
            "internal": True,
            "provenance": "subagent_completion",
            "run_id": run_id,
            "status": "completed",
        },
    )


def _internal_ai(content: str = "completion notification") -> AIMessage:
    return AIMessage(
        content=content,
        metadata={
            "internal": True,
            "provenance": "subagent_completion",
            "run_id": "run-test-1",
            "status": "completed",
        },
    )


@pytest.fixture(autouse=True)
def _clean_guard_and_budget_state():
    """Clear budget counters + repetition-guard history for this module's sessions."""
    for sid in (SID_DRAIN, SID_RESUME, SID_GUARD):
        for key in ("iteration_budget", "iteration_budget_used", *SESSION_STATE_KEYS):
            try:
                state_register_mem.delete_state(sid, key)
            except Exception:
                pass
    yield


@pytest.fixture()
def isolated_queue(tmp_path, monkeypatch):
    """Swap the module-level queue singleton to a temp-db SteeringQueue."""
    queue = SteeringQueue(store=PendingInjectionStore(db_path=tmp_path / "t.db"))
    monkeypatch.setitem(sq._QUEUE_HOLDER, "queue", queue)
    return queue


def test_drain_before_model_call(isolated_queue):
    injected = _completion_human()
    asyncio.run(sq.enqueue_steering(SID_DRAIN, injected))

    mw = SubagentCompletionDrainMiddleware()
    state = {"session_id": SID_DRAIN, "messages": [AIMessage(content="hi")]}

    result = asyncio.run(mw.abefore_model(state, None))
    assert result is not None
    msgs = result["messages"]
    assert len(msgs) == 1
    assert isinstance(msgs[0], HumanMessage)
    meta = msgs[0].metadata or {}
    assert meta.get("internal") is True
    assert meta.get("provenance") == "subagent_completion"
    assert msgs[0].text == injected.text

    # drain marks rows CONSUMED: no re-drain on the same session
    assert asyncio.run(mw.abefore_model(state, None)) is None


def test_internal_message_skips_guard_and_budget():
    """Internal completion messages bypass guard history and budget consumption."""
    internal_ai = _internal_ai()

    guard = OutputRepetitionGuard()
    request = SimpleNamespace(state={"session_id": SID_GUARD, "messages": [internal_ai]})
    assert guard._wrap_model_call_post(request, internal_ai) is None
    # skip happens BEFORE any history bookkeeping
    assert state_register_mem.get_state(SID_GUARD, _HISTORY_KEY, None) is None
    assert state_register_mem.get_state(SID_GUARD, _REASONING_HISTORY_KEY, None) is None

    budget = IterationBudget()
    request = SimpleNamespace(state={"session_id": SID_GUARD, "messages": [internal_ai]})
    assert budget._wrap_model_call_impl(request) is None
    assert state_register_mem.get_state(SID_GUARD, budget._USED_KEY, 0) == 0

    # negative control: a NORMAL message DOES consume budget (branch is live)
    normal = AIMessage(content="x" * 64)
    request_normal = SimpleNamespace(state={"session_id": SID_GUARD, "messages": [normal]})
    assert budget._wrap_model_call_impl(request_normal) is None
    assert state_register_mem.get_state(SID_GUARD, budget._USED_KEY, 0) == 1


def test_resume_drains_persisted_injection(tmp_path, monkeypatch):
    db = tmp_path / "resume.db"
    monkeypatch.setitem(
        sq._QUEUE_HOLDER,
        "queue",
        SteeringQueue(store=PendingInjectionStore(db_path=db)),
    )
    asyncio.run(
        sq.enqueue_steering(SID_RESUME, _completion_human("resume me", run_id="run-resume-1"))
    )

    # simulate restart: fresh queue instance over the same persisted db
    monkeypatch.setitem(
        sq._QUEUE_HOLDER,
        "queue",
        SteeringQueue(store=PendingInjectionStore(db_path=db)),
    )

    mw = SubagentCompletionDrainMiddleware()
    result = asyncio.run(mw.abefore_model({"session_id": SID_RESUME}, None))
    assert result is not None
    assert len(result["messages"]) == 1
    assert result["messages"][0].text == "resume me"

    # consumed exactly once after resume
    assert asyncio.run(mw.abefore_model({"session_id": SID_RESUME}, None)) is None
