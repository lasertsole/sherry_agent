"""Meeting-mode data models (spike, plan §2.3–2.6)."""

from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, Field

__all__ = [
    "InterruptType",
    "MeetingConclusion",
    "MeetingFlowStage",
    "MeetingMessage",
    "MeetingParticipant",
    "MeetingPhase",
    "MeetingStepSpec",
    "MeetingTurnDecision",
    "SpeakerKind",
    "MessageRole",
    "TaskFlowPlanFromMeeting",
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


class InterruptType(StrEnum):
    """The six user-interrupt classes (plan §2.5)."""

    SUPPLEMENT = "supplement"
    CORRECTION = "correction"
    SCOPE_CHANGE = "scope_change"
    PRIORITY_CHANGE = "priority_change"
    END_REQUEST = "end_request"
    IRRELEVANT = "irrelevant"


#: Interrupts that change what the meeting is discussing and therefore need a
#: local rollback of the affected phases; the other two only append/reorder.
ROLLBACK_INTERRUPTS = frozenset(
    {
        InterruptType.CORRECTION,
        InterruptType.SCOPE_CHANGE,
        InterruptType.PRIORITY_CHANGE,
    }
)

MessageRole = Literal["normal", "summary", "inquiry"]


class MeetingParticipant(BaseModel):
    """One seated participant (resolved from the expert registry)."""

    agent_id: str
    name: str
    persona_prompt: str
    join_order: int = 0


class MeetingPhase(BaseModel):
    """One phase of the selector's execution plan (business-ordered).

    ``status`` is 阶段 2's local-rollback marker: a scope change marks affected
    phases ``needs_rediscussion`` so the selector re-plans them instead of
    treating them as done.
    """

    id: str
    allowed_agent_ids: list[str] = Field(default_factory=list)
    prerequisites: list[str] = Field(default_factory=list)
    completion_criteria: str = ""
    status: str = "pending"


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


class MeetingStepSpec(BaseModel):
    """One step of the TaskFlow plan produced from a meeting (plan §5.1).

    ``step_id`` gives ``depends_on`` a stable target; when the model omits it,
    :func:`plan_to_steps` assigns a positional id and resolves dependencies by
    matching them against other steps' ids, tasks or expert names.
    """

    step_id: str = ""
    task: str
    expert: str
    depends_on: list[str] = Field(default_factory=list)
    reusable: bool = False


class TaskFlowPlanFromMeeting(BaseModel):
    """The TaskFlow-shaped output of a meeting conclusion (plan §5.1)."""

    description: str
    steps: list[MeetingStepSpec] = Field(default_factory=list)


class MeetingConclusion(BaseModel):
    """The host's structured conclusion once the discussion converges."""

    summary: str
    taskflow_plan: TaskFlowPlanFromMeeting
