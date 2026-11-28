"""User-interrupt classification and local rollback (plan §2.5, 阶段 2).

A user may interject at any point in a meeting. The HOST classifies the
interjection into one of six :class:`InterruptType` values; classes that change
what is being discussed (correction / scope change / priority change) mark the
affected execution-plan phases ``needs_rediscussion`` so the selector re-plans
them instead of treating them as settled.

The classifier is an injectable seam. The default is a deterministic keyword
classifier (no model dependency, stable in tests); a real deployment can pass
an LLM-backed classifier using the same protocol. This follows the plan's kill
criteria: mis-classification degrades to ``supplement`` (append only, no
rollback) rather than killing the meeting.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

from loguru import logger

from agent.meeting.models import (
    ROLLBACK_INTERRUPTS,
    InterruptType,
    MeetingPhase,
)

__all__ = [
    "InterruptRequest",
    "InterruptVerdict",
    "ClassifierFn",
    "classify_by_keywords",
    "make_classifier",
    "apply_local_rollback",
    "needs_rollback",
]


@dataclass(frozen=True)
class InterruptRequest:
    """One user interjection captured during a meeting."""

    content: str
    interrupt_id: str = ""


@dataclass(frozen=True)
class InterruptVerdict:
    """The host's classification of one interjection."""

    interrupt_type: InterruptType
    affected_phase_ids: list[str] = field(default_factory=list)
    reason: str = ""


class ClassifierFn(Protocol):
    """Injectable classifier: one interjection → one verdict."""

    def __call__(
        self,
        request: InterruptRequest,
        plan: list[MeetingPhase],
        history: list[dict[str, Any]],
    ) -> InterruptVerdict: ...


# Deterministic keyword rules, ordered by specificity. Each key is an
# InterruptType and the value is the set of (lowercased) trigger phrases.
_KEYWORD_RULES: dict[InterruptType, tuple[str, ...]] = {
    InterruptType.END_REQUEST: (
        "end the meeting",
        "stop the meeting",
        "that's enough",
        "let's finish",
        "conclude now",
        "结束会议",
        "先这样",
    ),
    InterruptType.SCOPE_CHANGE: (
        "scope change",
        "instead of",
        "change the scope",
        "use microservices",
        "switch to",
        "改用",
        "范围变更",
    ),
    InterruptType.PRIORITY_CHANGE: (
        "priority",
        "more important",
        "do this first",
        "deprioritize",
        "优先级",
        "先做",
    ),
    InterruptType.CORRECTION: (
        "correction",
        "that's wrong",
        "incorrect",
        "actually",
        "纠正",
        "不对",
    ),
    InterruptType.SUPPLEMENT: (
        "also",
        "additionally",
        "by the way",
        "one more thing",
        "补充",
        "另外",
    ),
}


def classify_by_keywords(
    request: InterruptRequest,
    plan: list[MeetingPhase],
    history: list[dict[str, Any]],
) -> InterruptVerdict:
    """Deterministic fallback classifier; unknown text is a supplement."""
    del history  # keyword rules do not need the discussion history
    content = request.content.lower()
    for kind, phrases in _KEYWORD_RULES.items():
        if any(phrase in content for phrase in phrases):
            affected = [phase.id for phase in plan] if kind in ROLLBACK_INTERRUPTS else []
            return InterruptVerdict(kind, affected, f"keyword match for {kind.value}")
    return InterruptVerdict(InterruptType.SUPPLEMENT, [], "no rollback keyword matched")


def make_classifier(llm: Any = None) -> ClassifierFn:
    """Return an LLM-backed classifier when given a model, else the keyword one.

    The LLM path asks for ``InterruptVerdict`` through the auxiliary model's
    structured output (the same seam as the selector). Any failure degrades to
    the keyword classifier so a broken model never blocks an interjection.
    """
    if llm is None:
        return classify_by_keywords

    def classify(
        request: InterruptRequest,
        plan: list[MeetingPhase],
        history: list[dict[str, Any]],
    ) -> InterruptVerdict:
        try:
            structured = llm.with_structured_output(InterruptVerdict)
            messages = [
                {
                    "role": "system",
                    "content": (
                        "You classify a user's interjection in a meeting. Pick one of "
                        + ", ".join(kind.value for kind in InterruptType)
                        + ". For correction/scope_change/priority_change list the ids of "
                        "the execution-plan phases that must be re-discussed."
                    ),
                },
                {
                    "role": "user",
                    "content": str(
                        {
                            "interjection": request.content,
                            "plan": [phase.model_dump() for phase in plan],
                            "recent": history[-6:],
                        }
                    ),
                },
            ]
            verdict = structured.invoke(messages)
            if isinstance(verdict, InterruptVerdict):
                return verdict
        except Exception as exc:  # pragma: no cover - provider failure path
            logger.warning("meeting interrupt classifier failed: {}", exc)
        return classify_by_keywords(request, plan, history)

    return classify


def needs_rollback(verdict: InterruptVerdict) -> bool:
    """Whether this verdict changes the discussion enough to re-plan phases."""
    return verdict.interrupt_type in ROLLBACK_INTERRUPTS


def apply_local_rollback(
    plan: list[MeetingPhase], affected_phase_ids: list[str]
) -> list[MeetingPhase]:
    """Mark affected phases (and their dependents) ``needs_rediscussion``.

    A phase that depends on an affected phase is also invalidated transitively:
    re-discussing a prerequisite changes the basis its dependents were built
    on. Phases are returned as new instances so the caller's plan is untouched.
    """
    affected = set(affected_phase_ids)
    changed = True
    while changed:
        changed = False
        for phase in plan:
            if phase.id not in affected and any(dep in affected for dep in phase.prerequisites):
                affected.add(phase.id)
                changed = True
    return [
        phase.model_copy(update={"status": "needs_rediscussion"}) if phase.id in affected else phase
        for phase in plan
    ]
