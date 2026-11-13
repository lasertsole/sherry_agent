"""A blocked step must not be announced as "completed successfully".

Three channels carry a child's outcome to its requester: the human-facing
completion notice, the third-path marker (``[subagent:{name} {status}]``) and
the sub→sub internal injection. A budget-exhausted goal loop and a StepJudge
BLOCK both leave the step blocked; every channel must say ``blocked`` and the
judge's reason must survive (it is read from the post-resume step state, since
the resume answer's first line never names it).
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agent.tools.subagent.announce import delivery as delivery_mod
from agent.tools.subagent.announce.completion_message import (
    STATUS_BLOCKED,
    STATUS_COMPLETED,
)
from agent.tools.subagent.announce.delivery import (
    _deliver_completion_message,
    _deliver_internal_injection,
    _remember_blocked_step,
    _resolve_builder_status,
)
from agent.tools.subagent.types.delivery import DeliveryContext
from agent.tools.subagent.types.registry import RunOutcome, RunOutcomeStatus

pytestmark = [pytest.mark.unit]


@pytest.fixture(autouse=True)
def _clear_resume_notes():
    delivery_mod._RESUME_BLOCKED.clear()
    yield
    delivery_mod._RESUME_BLOCKED.clear()


def _run(status: RunOutcomeStatus, *, run_id: str = "run-1") -> SimpleNamespace:
    return SimpleNamespace(
        run_id=run_id,
        child_session_key="agent:main:subagent:c1",
        execution=SimpleNamespace(outcome=RunOutcome(status=status)),
    )


def _ctx(**overrides) -> DeliveryContext:
    defaults = dict(
        requester_session_key="agent:main:session:user1",
        child_session_key="agent:main:subagent:child",
        child_label="worker-1",
        task="test task",
        result_text="partial result",
        outcome=RunOutcome(status=RunOutcomeStatus.OK),
        run_id="run-1",
        is_requester_subagent=False,
    )
    defaults.update(overrides)
    return DeliveryContext(**defaults)


async def _published(ctx: DeliveryContext, deliver) -> str:
    bus = MagicMock()
    bus.publish_internal = AsyncMock()
    with patch("agent.tools.subagent.events.get_event_bus", return_value=bus):
        await deliver(ctx)
    return bus.publish_internal.call_args[0][0].content


def test_budget_exhausted_maps_to_blocked():
    assert _resolve_builder_status(_run(RunOutcomeStatus.BUDGET_EXHAUSTED)) == STATUS_BLOCKED


def test_a_blocked_resume_note_maps_an_ok_run_to_blocked():
    delivery_mod._remember_blocked_resume("run-1", "StepJudge blocked: no evidence")

    assert _resolve_builder_status(_run(RunOutcomeStatus.OK)) == STATUS_BLOCKED


def test_an_ok_run_without_a_note_stays_completed():
    assert _resolve_builder_status(_run(RunOutcomeStatus.OK)) == STATUS_COMPLETED


@pytest.mark.asyncio
async def test_budget_exhausted_notice_reports_blocked_not_success():
    content = await _published(
        _ctx(outcome=RunOutcome(status=RunOutcomeStatus.BUDGET_EXHAUSTED)),
        _deliver_completion_message,
    )

    assert "Status: blocked (budget exhausted" in content
    assert "completed successfully" not in content


@pytest.mark.asyncio
async def test_a_judge_blocked_step_carries_its_reason_in_the_notice():
    delivery_mod._remember_blocked_resume("run-1", "StepJudge blocked: no evidence")

    content = await _published(_ctx(), _deliver_completion_message)

    assert "Status: blocked (StepJudge blocked: no evidence)" in content
    assert "completed successfully" not in content


@pytest.mark.asyncio
async def test_the_internal_injection_uses_the_same_blocked_vocabulary():
    content = await _published(
        _ctx(
            is_requester_subagent=True,
            outcome=RunOutcome(status=RunOutcomeStatus.BUDGET_EXHAUSTED),
        ),
        _deliver_internal_injection,
    )

    assert ": blocked" in content.splitlines()[0]


@pytest.mark.asyncio
async def test_the_post_resume_step_state_supplies_the_block_reason(monkeypatch):
    from agent.tools.taskflow.registry import store_sqlite as taskflow_store

    async def _flow(flow_id, session_id):
        assert flow_id == "flow-1" and session_id == "sess-parent"
        return {
            "state": {
                "steps": [
                    {
                        "step_id": "s1",
                        "child_session_key": "agent:main:subagent:c1",
                        "status": "blocked",
                        "block_reason": "StepJudge blocked: no evidence",
                    }
                ]
            }
        }

    monkeypatch.setattr(taskflow_store, "get_flow", _flow)

    await _remember_blocked_step(_run(RunOutcomeStatus.OK), "flow-1", "sess-parent")

    assert delivery_mod._blocked_resume_reason("run-1") == "StepJudge blocked: no evidence"


@pytest.mark.asyncio
async def test_a_dispatched_retry_is_not_recorded_as_blocked(monkeypatch):
    """StepJudge RETRY re-dispatches the step; it is running, not blocked."""
    from agent.tools.taskflow.registry import store_sqlite as taskflow_store

    async def _flow(flow_id, session_id):
        return {
            "state": {
                "steps": [
                    {
                        "step_id": "s1",
                        "child_session_key": "agent:main:subagent:c1",
                        "status": "dispatched",
                    }
                ]
            }
        }

    monkeypatch.setattr(taskflow_store, "get_flow", _flow)

    await _remember_blocked_step(_run(RunOutcomeStatus.OK), "flow-1", "sess-parent")

    assert delivery_mod._blocked_resume_reason("run-1") is None
