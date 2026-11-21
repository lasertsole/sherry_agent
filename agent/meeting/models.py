"""Meeting-mode data models (spike, plan §2.3–2.6)."""

from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, Field

__all__ = [
    "MeetingFlowStage",
    "MeetingMessage",
    "MeetingParticipant",
    "MeetingPhase",
    "MeetingTurnDecision",
    "SpeakerKind",
    "MessageRole",
]


class MeetingFlowStage(StrEnum):
    """The five flow stages (plan §2.4); non-dispatching stages host-speaks."""

    REQUIREMENT_CONFIRMATION = "requirement_confirmation"
    DISPATCHING = "dispatching"
    USER_INTERRUPT = "user_interrupt"
    RECOVERY = "recovery"
    END_CONFIRMATION = "end_confirmation"


class SpeakerKind(StrEnum):
    HOST = "host"
    AGENT = "agent"
    USER = "user"


MessageRole = Literal["normal", "summary", "inquiry"]


class MeetingParticipant(BaseModel):
    """One seated participant (resolved from the expert registry)."""

    agent_id: str
    name: str
    persona_prompt: str
    join_order: int = 0


class MeetingPhase(BaseModel):
    """One phase of the selector's execution plan (business-ordered)."""

    id: str
    allowed_agent_ids: list[str] = Field(default_factory=list)
    prerequisites: list[str] = Field(default_factory=list)
    completion_criteria: str = ""


class MeetingTurnDecision(BaseModel):
    """The selector's structured verdict for ONE turn (plan §2.3)."""

    phase: str = MeetingFlowStage.DISPATCHING.value
    next_speaker_id: str | None = None
    reason: str = ""
    execution_plan: list[MeetingPhase] = Field(default_factory=list)
    interrupt_type: str | None = None
    affected_phase_ids: list[str] = Field(default_factory=list)


class MeetingMessage(BaseModel):
    """One appended utterance (plan §2.6)."""

    meeting_id: str
    sequence: int
    speaker_kind: str
    speaker_id: str
    speaker_name: str
    message_role: str = "normal"
    content: str
    timestamp: int = 0
