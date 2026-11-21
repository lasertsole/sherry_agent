"""The meeting discussion loop: convergence, recovery and the round cap.

The selector and the participants are FAKES: the spike verifies the loop's
stage machine, the persistence calls and the round budget, not the model.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import pytest_asyncio

import agent.meeting.persistence as persistence
from agent.meeting.flow import run_meeting
from agent.meeting.models import MeetingParticipant, MeetingTurnDecision

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]


@pytest_asyncio.fixture
async def store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> persistence.MeetingStore:
    persistence._reset_store_for_tests(tmp_path / "registry.db")
    store = persistence.MeetingStore()
    await store.ensure_db()
    return store


PARTICIPANTS = [
    MeetingParticipant(
        agent_id="fe", name="Frontend", persona_prompt="frontend persona", join_order=1
    ),
    MeetingParticipant(
        agent_id="be", name="Backend", persona_prompt="backend persona", join_order=2
    ),
]


def _fake_selector(order: list[str | None]):
    """A selector that walks a scripted speaker list, then converges."""
    calls: list[dict] = []

    def selector(history, participants):
        calls.append({"history_len": len(history), "participants": len(participants)})
        if not order:
            return MeetingTurnDecision(reason="converged")
        return MeetingTurnDecision(
            next_speaker_id=order.pop(0),
            reason=f"turn {len(calls)}",
            execution_plan=[{"id": "p1", "allowedAgentIds": order.copy(), "prerequisites": []}],
        )

    selector.calls = calls
    return selector


def _fake_speaker(statements: dict[str, list[str]]):
    def speak(participant, history, topic):
        return statements[participant.agent_id].pop(0)

    return speak


async def test_converged_meeting_persists_full_flow(store):
    selector = _fake_selector(["fe", "be", None])
    speaker = _fake_speaker({"fe": ["use React"], "be": ["use FastAPI"]})

    result = await run_meeting(
        store,
        session_id="sess",
        topic="architecture",
        participants=PARTICIPANTS,
        selector=selector,
        participant_speak=speaker,
    )

    assert result["rounds"] == 2
    assert result["meeting"]["status"] == "ended"
    assert result["meeting"]["flow_stage"] == "end_confirmation"
    messages = await store.list_messages(result["meeting_id"])
    kinds = [(m["speaker_kind"], m["speaker_id"], m["message_role"]) for m in messages]
    assert kinds[0] == ("host", "main", "normal")
    assert ("agent", "fe", "normal") in kinds
    assert ("agent", "be", "normal") in kinds
    assert kinds[-1] == ("host", "main", "summary")
    # the selector saw the growing history and the execution plan was persisted
    assert [call["history_len"] for call in selector.calls] == [1, 2, 3]
    meeting = await store.get_meeting(result["meeting_id"])
    assert meeting["execution_plan"][0]["id"] == "p1"
    assert "React" in "".join(m["content"] for m in messages)


async def test_round_cap_forces_the_summary(store):
    selector = _fake_selector(["fe"] * 50)
    speaker = _fake_speaker({"fe": ["still working"] * 50})

    result = await run_meeting(
        store,
        session_id="sess",
        topic="endless",
        participants=PARTICIPANTS,
        selector=selector,
        participant_speak=speaker,
        max_rounds=3,
    )

    assert result["rounds"] == 3
    messages = await store.list_messages(result["meeting_id"])
    assert any("round budget of 3" in m["content"] for m in messages)
    assert result["meeting"]["status"] == "ended"


async def test_unknown_speaker_enters_recovery_then_recovers(store):
    selector = _fake_selector(["ghost", "fe", None])
    speaker = _fake_speaker({"fe": ["recovered statement"]})

    result = await run_meeting(
        store,
        session_id="sess",
        topic="t",
        participants=PARTICIPANTS,
        selector=selector,
        participant_speak=speaker,
    )

    assert result["rounds"] >= 2
    messages = await store.list_messages(result["meeting_id"])
    assert any("not seated" in m["content"] for m in messages)
    assert any("recovered statement" in m["content"] for m in messages)
    meeting = await store.get_meeting(result["meeting_id"])
    assert meeting["status"] == "ended"


async def test_selector_failure_degrades_to_convergence(store):
    def broken_selector(history, participants):
        raise RuntimeError("provider down")

    result = await run_meeting(
        store,
        session_id="sess",
        topic="t",
        participants=PARTICIPANTS,
        selector=broken_selector,
        participant_speak=_fake_speaker({"fe": ["x"]}),
    )

    assert result["meeting"]["status"] == "ended"
