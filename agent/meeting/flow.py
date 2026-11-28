"""The meeting discussion loop (spike, plan 阶段 1 MVP).

One call runs the whole meeting against INJECTED selector / participant seams:

1. the HOST opens (requirement confirmation — a host statement, no selector);
2. DISPATCHING rounds: the selector names the next speaker, that participant
   speaks, the utterance is appended; ``next_speaker_id=None`` ends the loop;
3. the HOST summarizes and asks the user to confirm the end (END_CONFIRMATION);
4. the meeting is persisted as ``ended`` with its summary and round count.

The 40-round budget (plan §13.7) caps the dispatching loop — the counter is
persisted per meeting, and hitting it forces the summary.
"""

from __future__ import annotations

import json
import time
import uuid
from typing import Any

from loguru import logger

from agent.meeting.emit import emit_meeting_event
from agent.meeting.host import phase_allows_speaker
from agent.meeting.interrupt import (
    InterruptRequest,
    InterruptVerdict,
    apply_local_rollback,
    classify_by_keywords,
    needs_rollback,
)
from agent.meeting.models import (
    InterruptType,
    MeetingFlowStage,
    MeetingMessage,
    MeetingParticipant,
    MeetingPhase,
    MeetingTurnDecision,
    SpeakerKind,
)
from agent.meeting.persistence import MeetingStore
from agent.meeting.participant import ParticipantFn, make_participants
from agent.meeting.selector import SelectorFn, make_selector

__all__ = [
    "MAX_ROUNDS",
    "MeetingRoundLimitError",
    "apply_interruption",
    "run_meeting",
    "run_meeting_with_interrupts",
]

MAX_ROUNDS = 40
HOST_AGENT_ID = "main"
HOST_NAME = "Host"

_OPENING_PROMPT = (
    "The meeting on '{topic}' is open. Participants: {names}. "
    "State the goal, the decision the meeting must reach, and invite the first "
    "opinions in the order the execution plan will propose."
)
_SUMMARY_PROMPT = (
    "The discussion on '{topic}' has converged. Summarize the shared conclusion, "
    "the decisions made and the proposed next steps."
)


class MeetingRoundLimitError(RuntimeError):
    """Raised internally when the round cap forces the summary; never escapes."""


async def run_meeting(
    store: MeetingStore,
    *,
    session_id: str,
    topic: str,
    participants: list[MeetingParticipant],
    selector: SelectorFn | None = None,
    participant_speak: ParticipantFn | None = None,
    max_rounds: int = MAX_ROUNDS,
    meeting_id: str | None = None,
) -> dict[str, Any]:
    """Run one meeting to convergence (or the round cap) and persist it.

    Returns the final meeting row plus the conclusion payload the caller can
    turn into a TaskFlow plan (阶段 4).
    """
    if selector is None:
        selector = make_selector()
    if participant_speak is None:
        participant_speak = make_participants()
    # A route may drive an EXISTING in_progress row (the CRUD create); only the
    # direct-call path mints its own id and row.
    participants_payload = [
        {"agent_id": p.agent_id, "name": p.name, "join_order": p.join_order} for p in participants
    ]
    if meeting_id is None:
        meeting_id = uuid.uuid4().hex
        await store.create_meeting(
            meeting_id,
            session_id=session_id,
            topic=topic,
            participants=participants_payload,
            host_agent_id=HOST_AGENT_ID,
        )
    by_id = {p.agent_id: p for p in participants}
    history: list[dict[str, Any]] = []
    decision: MeetingTurnDecision | None = None

    async def append(
        kind: str, speaker_id: str, speaker_name: str, content: str, role: str = "normal"
    ) -> None:
        message = MeetingMessage(
            meeting_id=meeting_id,
            sequence=await store.next_sequence(meeting_id),
            speaker_kind=kind,
            speaker_id=speaker_id,
            speaker_name=speaker_name,
            message_role=role,
            content=content,
            timestamp=int(time.time()),
        )
        await store.append_message(message)
        history.append(
            {
                "speaker_kind": kind,
                "speaker_id": speaker_id,
                "speaker_name": speaker_name,
                "content": content,
                "role": role,
            }
        )

    # Stage 1 — the host opens (no selector: a non-dispatching stage speaks).
    await store.update_meeting(
        meeting_id, flow_stage=MeetingFlowStage.REQUIREMENT_CONFIRMATION.value
    )
    await append(
        SpeakerKind.HOST.value,
        HOST_AGENT_ID,
        HOST_NAME,
        _OPENING_PROMPT.format(topic=topic, names=", ".join(p.name for p in participants)),
    )

    # Stage 2 — the dispatching loop.
    await store.update_meeting(meeting_id, flow_stage=MeetingFlowStage.DISPATCHING.value)
    rounds = 0
    try:
        while rounds < max_rounds:
            try:
                decision = selector(history, participants_payload)
            except Exception as exc:  # a broken selector must not kill the meeting
                logger.warning("meeting selector failed: {}", exc)
                decision = MeetingTurnDecision(reason=f"selector failed: {exc}")
            if decision.execution_plan:
                await store.update_meeting(
                    meeting_id,
                    execution_plan_json=json.dumps(
                        [phase.model_dump() for phase in decision.execution_plan],
                        ensure_ascii=False,
                    ),
                )
            if not decision.next_speaker_id:
                break
            participant = by_id.get(decision.next_speaker_id)
            if participant is None:
                # RECOVERY: the selector named an unknown/absent speaker; the
                # host asks it to pick again, bounded by the same round budget.
                await store.update_meeting(meeting_id, flow_stage=MeetingFlowStage.RECOVERY.value)
                await append(
                    SpeakerKind.HOST.value,
                    HOST_AGENT_ID,
                    HOST_NAME,
                    f"The proposed speaker {decision.next_speaker_id!r} is not seated; "
                    "re-selecting among the participants.",
                )
                await store.update_meeting(
                    meeting_id, flow_stage=MeetingFlowStage.DISPATCHING.value
                )
                rounds += 1
                continue
            await store.update_meeting(meeting_id, iteration_count=rounds + 1)
            statement = participant_speak(participant, history, topic)
            await append(SpeakerKind.AGENT.value, participant.agent_id, participant.name, statement)
            rounds += 1
        else:
            raise MeetingRoundLimitError(max_rounds)
    except MeetingRoundLimitError:
        await append(
            SpeakerKind.HOST.value,
            HOST_AGENT_ID,
            HOST_NAME,
            f"The round budget of {max_rounds} is exhausted; forcing the summary.",
        )

    # Stage 3 — the host summarizes and asks for the end confirmation.
    await store.update_meeting(meeting_id, flow_stage=MeetingFlowStage.END_CONFIRMATION.value)
    summary = f"Discussion on '{topic}' converged after {rounds} participant statements."
    if decision is not None and decision.reason:
        summary += f" Convergence reason: {decision.reason}"
    await append(SpeakerKind.HOST.value, HOST_AGENT_ID, HOST_NAME, summary, role="summary")

    await store.update_meeting(
        meeting_id,
        status="ended",
        ended_at=int(time.time()),
        iteration_count=rounds,
        final_summary=summary,
        flow_stage=MeetingFlowStage.END_CONFIRMATION.value,
    )
    meeting = await store.get_meeting(meeting_id)
    return {
        "meeting": meeting,
        "meeting_id": meeting_id,
        "rounds": rounds,
        "conclusion": {
            "summary": summary,
            "taskflow_plan": {
                "description": f"Plan from the meeting on '{topic}'",
                # 阶段 4 turns this into real steps; the spike carries the
                # selector's phases as the dependency sketch.
                "phases": [p.model_dump() for p in (decision.execution_plan if decision else [])],
            },
        },
    }


async def apply_interruption(
    store: MeetingStore,
    meeting_id: str,
    content: str,
    *,
    classifier: Any = None,
) -> dict[str, Any]:
    """Classify one user interjection and apply the 阶段 2 rollback contract.

    Returns a payload describing the verdict, the (possibly rolled-back) plan
    and the new flow stage. Schema:

    ``{"verdict": InterruptVerdict, "plan": list[MeetingPhase],
       "message": MeetingMessage|None, "flow_stage": str,
       "should_end": bool, "rolled_back": list[str]}``

    ``should_end`` is true only for ``end_request``; the caller then drives the
    END_CONFIRMATION summary. Other classes append a ``user`` message and, when
    rollback applies, mark affected phases ``needs_rediscussion`` for the
    selector to re-plan on the next round.
    """
    meeting = await store.get_meeting(meeting_id)
    if meeting is None:
        raise KeyError(f"meeting {meeting_id!r} not found")
    plan = [MeetingPhase.model_validate(phase) for phase in (meeting.get("execution_plan") or [])]
    # Rebuild a minimal history so the classifier sees what has been said.
    stored = await store.list_messages(meeting_id)
    history = [
        {
            "speaker_kind": row["speaker_kind"],
            "speaker_id": row["speaker_id"],
            "speaker_name": row["speaker_name"],
            "content": row["content"],
            "role": row["message_role"],
        }
        for row in stored
    ]
    classify = classifier or classify_by_keywords
    verdict: InterruptVerdict = classify(InterruptRequest(content=content), plan, history)

    await store.update_meeting(meeting_id, flow_stage=MeetingFlowStage.USER_INTERRUPT.value)
    message = MeetingMessage(
        meeting_id=meeting_id,
        sequence=await store.next_sequence(meeting_id),
        speaker_kind=SpeakerKind.USER.value,
        speaker_id="user",
        speaker_name="User",
        message_role="inquiry",
        content=content,
        timestamp=int(time.time()),
    )
    await store.append_message(message)

    rolled_back: list[str] = []
    final_stage = MeetingFlowStage.DISPATCHING.value
    should_end = verdict.interrupt_type is InterruptType.END_REQUEST
    if should_end:
        final_stage = MeetingFlowStage.END_CONFIRMATION.value
    elif needs_rollback(verdict):
        plan = apply_local_rollback(plan, verdict.affected_phase_ids)
        rolled_back = [p.id for p in plan if p.status == "needs_rediscussion"]
        await store.update_meeting(
            meeting_id,
            execution_plan_json=[p.model_dump() for p in plan],
        )
        final_stage = MeetingFlowStage.RECOVERY.value
    await store.update_meeting(meeting_id, flow_stage=final_stage)
    return {
        "verdict": verdict,
        "plan": plan,
        "message": message,
        "flow_stage": final_stage,
        "should_end": should_end,
        "rolled_back": rolled_back,
    }


async def run_meeting_with_interrupts(
    store: MeetingStore,
    *,
    session_id: str,
    topic: str,
    participants: list[MeetingParticipant],
    interrupts: list[str] | None = None,
    selector: SelectorFn | None = None,
    participant_speak: ParticipantFn | None = None,
    classifier: Any = None,
    max_rounds: int = MAX_ROUNDS,
    meeting_id: str | None = None,
) -> dict[str, Any]:
    """阶段 2 variant: run the loop, draining queued interrupts between rounds.

    The interrupt queue is consumed FIFO; each interjection is classified and
    applied through :func:`apply_interruption`. A rollback verdict marks the
    affected plan phases ``needs_rediscussion`` and the selector sees the
    updated plan on the next round. ``end_request`` stops the loop early and
    forces the host summary. The spike's :func:`run_meeting` stays untouched
    for callers that do not drive interrupts.
    """
    if selector is None:
        selector = make_selector()
    if participant_speak is None:
        participant_speak = make_participants()
    pending = list(interrupts or [])
    participants_payload = [
        {"agent_id": p.agent_id, "name": p.name, "join_order": p.join_order} for p in participants
    ]
    if meeting_id is None:
        meeting_id = uuid.uuid4().hex
        await store.create_meeting(
            meeting_id,
            session_id=session_id,
            topic=topic,
            participants=participants_payload,
            host_agent_id=HOST_AGENT_ID,
        )
    by_id = {p.agent_id: p for p in participants}
    history: list[dict[str, Any]] = []
    decision: MeetingTurnDecision | None = None
    plan: list[MeetingPhase] = []

    async def append(
        kind: str, speaker_id: str, speaker_name: str, content: str, role: str = "normal"
    ) -> None:
        message = MeetingMessage(
            meeting_id=meeting_id,
            sequence=await store.next_sequence(meeting_id),
            speaker_kind=kind,
            speaker_id=speaker_id,
            speaker_name=speaker_name,
            message_role=role,
            content=content,
            timestamp=int(time.time()),
        )
        await store.append_message(message)
        history.append(
            {
                "speaker_kind": kind,
                "speaker_id": speaker_id,
                "speaker_name": speaker_name,
                "content": content,
                "role": role,
            }
        )
        if kind == SpeakerKind.AGENT.value:
            emit_meeting_event(
                meeting_id,
                {
                    "event": "meeting_speaker_changed",
                    "speaker_id": speaker_id,
                    "speaker_name": speaker_name,
                },
            )
        emit_meeting_event(
            meeting_id,
            {
                "event": "meeting_message_completed",
                "sequence": message.sequence,
                "speaker_kind": kind,
                "speaker_id": speaker_id,
                "speaker_name": speaker_name,
                "content": content,
                "message_role": role,
            },
        )

    async def drain_interrupts() -> bool:
        """Apply all queued interrupts; return True if an end was requested."""
        nonlocal plan
        requested_end = False
        while pending:
            result = await apply_interruption(
                store, meeting_id, pending.pop(0), classifier=classifier
            )
            plan = result["plan"]
            history.append(
                {
                    "speaker_kind": SpeakerKind.USER.value,
                    "speaker_id": "user",
                    "speaker_name": "User",
                    "content": result["message"].content,
                    "role": "inquiry",
                }
            )
            requested_end = requested_end or bool(result["should_end"])
        return requested_end

    await store.update_meeting(
        meeting_id, flow_stage=MeetingFlowStage.REQUIREMENT_CONFIRMATION.value
    )
    await append(
        SpeakerKind.HOST.value,
        HOST_AGENT_ID,
        HOST_NAME,
        _OPENING_PROMPT.format(topic=topic, names=", ".join(p.name for p in participants)),
    )
    await store.update_meeting(meeting_id, flow_stage=MeetingFlowStage.DISPATCHING.value)

    rounds = 0
    end_requested = await drain_interrupts()
    try:
        while rounds < max_rounds and not end_requested:
            decision = selector(history, participants_payload)
            if decision.execution_plan:
                plan = list(decision.execution_plan)
                await store.update_meeting(
                    meeting_id,
                    execution_plan_json=[p.model_dump() for p in plan],
                )
            if not decision.next_speaker_id:
                break
            # Phase allow-list gate (阶段 2): if the selector names a speaker
            # outside the active phase, treat it as a RECOVERY round.
            if not phase_allows_speaker(decision.phase, decision.next_speaker_id, plan):
                await store.update_meeting(meeting_id, flow_stage=MeetingFlowStage.RECOVERY.value)
                await append(
                    SpeakerKind.HOST.value,
                    HOST_AGENT_ID,
                    HOST_NAME,
                    f"{decision.next_speaker_id!r} is not allowed in phase "
                    f"{decision.phase!r}; re-selecting.",
                )
                await store.update_meeting(
                    meeting_id, flow_stage=MeetingFlowStage.DISPATCHING.value
                )
                rounds += 1
                end_requested = await drain_interrupts()
                continue
            participant = by_id.get(decision.next_speaker_id)
            if participant is None:
                await store.update_meeting(meeting_id, flow_stage=MeetingFlowStage.RECOVERY.value)
                await append(
                    SpeakerKind.HOST.value,
                    HOST_AGENT_ID,
                    HOST_NAME,
                    f"The proposed speaker {decision.next_speaker_id!r} is not seated; "
                    "re-selecting among the participants.",
                )
                await store.update_meeting(
                    meeting_id, flow_stage=MeetingFlowStage.DISPATCHING.value
                )
                rounds += 1
                end_requested = await drain_interrupts()
                continue
            await store.update_meeting(meeting_id, iteration_count=rounds + 1)
            statement = participant_speak(participant, history, topic)
            await append(SpeakerKind.AGENT.value, participant.agent_id, participant.name, statement)
            rounds += 1
            end_requested = await drain_interrupts()
        else:
            if not end_requested:
                raise MeetingRoundLimitError(max_rounds)
    except MeetingRoundLimitError:
        await append(
            SpeakerKind.HOST.value,
            HOST_AGENT_ID,
            HOST_NAME,
            f"The round budget of {max_rounds} is exhausted; forcing the summary.",
        )

    await store.update_meeting(meeting_id, flow_stage=MeetingFlowStage.END_CONFIRMATION.value)
    summary = f"Discussion on '{topic}' converged after {rounds} participant statements."
    if decision is not None and decision.reason:
        summary += f" Convergence reason: {decision.reason}"
    await append(SpeakerKind.HOST.value, HOST_AGENT_ID, HOST_NAME, summary, role="summary")
    emit_meeting_event(meeting_id, {"event": "meeting_summary_draft", "summary": summary})
    await store.update_meeting(
        meeting_id,
        status="ended",
        ended_at=int(time.time()),
        iteration_count=rounds,
        final_summary=summary,
        flow_stage=MeetingFlowStage.END_CONFIRMATION.value,
    )
    return {
        "meeting": await store.get_meeting(meeting_id),
        "meeting_id": meeting_id,
        "rounds": rounds,
        "conclusion": {
            "summary": summary,
            "taskflow_plan": {
                "description": f"Plan from the meeting on '{topic}'",
                "phases": [p.model_dump() for p in plan],
            },
        },
    }
