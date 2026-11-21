"""A participant's statement (spike degradation, plan §15 kill criteria).

The plan's primary mechanism (§13.1) swaps the main agent session's
``StateKey.SYSTEM_PROMPT`` per participant turn. The spike uses the ALLOWED
degradation instead: one LLM call per statement with the participant persona +
the shared discussion history. It verifies the discussion loop, the message
model and the persistence without touching the live session registers; the
persona-swap model is validated after the loop itself proves value.
"""

from __future__ import annotations

from typing import Any, Protocol

from agent.meeting.models import MeetingParticipant

__all__ = ["ParticipantFn", "make_participants", "PARTICIPANT_SYSTEM_PROMPT"]

PARTICIPANT_SYSTEM_PROMPT = """You are {name}, participating in a meeting about: {topic}

Previous statements:
{history}

Give your domain opinion based on the statements so far. If you agree or
disagree with earlier points, say why. Be concrete and brief."""


class ParticipantFn(Protocol):
    """The injectable participant seam: one seat + history → one statement."""

    def __call__(
        self, participant: MeetingParticipant, history: list[dict[str, Any]], topic: str
    ) -> str: ...


def make_participants(llm: Any = None) -> ParticipantFn:
    """The real participant call bound to the auxiliary model."""

    def speak(participant: MeetingParticipant, history: list[dict[str, Any]], topic: str) -> str:
        if llm is None:
            from models.LLMs.auxiliary_llm.core import build_auxiliary_llm

            model = build_auxiliary_llm(temperature=0, thinking=False)
        else:
            model = llm
        rendered = "\n".join(
            f"[{item['speaker_name'] or item['speaker_kind']}]: {item['content']}"
            for item in history
        )
        messages = [
            {
                "role": "system",
                "content": PARTICIPANT_SYSTEM_PROMPT.format(
                    name=participant.name, topic=topic, history=rendered or "(none yet)"
                ),
            },
            {"role": "user", "content": "It is your turn to speak."},
        ]
        return str(model.invoke(messages).content)

    return speak
