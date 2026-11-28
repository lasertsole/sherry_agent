"""Meeting conclusion → TaskFlow plan (plan §5.1, 阶段 4).

The host's structured conclusion (:class:`MeetingConclusion`) is turned into a
TaskFlow-shaped plan: each :class:`MeetingStepSpec` maps to a step id, task,
``depends_on`` and a registered ``expert`` name. ``reusable`` is a meeting-side
hint only — TaskFlow's ``new_step`` has no such column, so it is retained in the
meeting's conclusion payload rather than pushed into the flow.

The structured-output seam is injectable: the real path uses an auxiliary
model's ``with_structured_output(MeetingConclusion)``; tests pass a fake. Any
model failure degrades to a deterministic conclusion built from the selector's
phases so a meeting always closes with an executable plan.
"""

from __future__ import annotations

from typing import Any, Protocol

from loguru import logger

from agent.meeting.models import (
    MeetingConclusion,
    MeetingPhase,
    MeetingStepSpec,
    TaskFlowPlanFromMeeting,
)

__all__ = [
    "ConclusionFn",
    "build_conclusion",
    "conclusion_from_phases",
    "make_conclusion_builder",
    "plan_to_steps",
]


class ConclusionFn(Protocol):
    """Injectable conclusion seam: history + phases + topic → conclusion."""

    def __call__(
        self,
        history: list[dict[str, Any]],
        phases: list[MeetingPhase],
        topic: str,
        summary: str,
    ) -> MeetingConclusion: ...


CONCLUSION_SYSTEM_PROMPT = """You are a meeting host closing a discussion. Produce a
structured conclusion with a short summary and a TaskFlow plan. Each step has a
task, the name of a registered expert to execute it, depends_on step ids, and a
reusable hint. Steps must respect the business dependencies from the discussion."""


def conclusion_from_phases(
    topic: str, summary: str, phases: list[MeetingPhase]
) -> MeetingConclusion:
    """Deterministic fallback: one step per phase, chained by prerequisites."""
    id_to_step: dict[str, str] = {}
    steps: list[MeetingStepSpec] = []
    for phase in phases:
        step_id = f"step-{phase.id}"
        id_to_step[phase.id] = step_id
        expert = phase.allowed_agent_ids[0] if phase.allowed_agent_ids else "main"
        steps.append(
            MeetingStepSpec(
                step_id=step_id,
                task=phase.completion_criteria or f"Address phase {phase.id}",
                expert=expert,
                depends_on=[id_to_step[d] for d in phase.prerequisites if d in id_to_step],
            )
        )
    return MeetingConclusion(
        summary=summary,
        taskflow_plan=TaskFlowPlanFromMeeting(
            description=f"Plan from the meeting on '{topic}'", steps=steps
        ),
    )


def plan_to_steps(conclusion: MeetingConclusion) -> list[dict[str, Any]]:
    """Serialize the conclusion's step specs into TaskFlow step dicts.

    Each dict carries ``step_id`` / ``task`` / ``depends_on`` / ``status`` plus
    the registered ``expert`` name consumed by ``taskflow_dispatch``'s
    ``_resolve_expert``. ``reusable`` stays out of the flow (not a native field).

    Dependencies are resolved to concrete step ids: a spec's ``depends_on`` may
    name another step's ``step_id``, its ``task``, or its ``expert``; unresolved
    references are dropped so TaskFlow's DAG validation never rejects the plan.
    """
    from agent.tools.taskflow.tools._shared import new_step

    specs = conclusion.taskflow_plan.steps
    ids = [spec.step_id or f"step-{idx + 1}" for idx, spec in enumerate(specs)]
    # Maps any alias (id, task, expert) a model might use in depends_on.
    aliases: dict[str, str] = {}
    for spec, step_id in zip(specs, ids):
        for alias in (step_id, spec.task, spec.expert):
            aliases.setdefault(alias, step_id)

    steps: list[dict[str, Any]] = []
    for spec, step_id in zip(specs, ids):
        deps: list[str] = []
        for dep in spec.depends_on:
            resolved = aliases.get(dep)
            if resolved and resolved != step_id and resolved not in deps:
                deps.append(resolved)
        steps.append(new_step(step_id, spec.task, depends_on=deps, expert=spec.expert))
    return steps


def make_conclusion_builder(llm: Any = None) -> ConclusionFn:
    """Return an LLM-backed conclusion builder, or the deterministic fallback."""
    if llm is None:
        return lambda history, phases, topic, summary: conclusion_from_phases(
            topic, summary, phases
        )

    def build(
        history: list[dict[str, Any]],
        phases: list[MeetingPhase],
        topic: str,
        summary: str,
    ) -> MeetingConclusion:
        try:
            structured = llm.with_structured_output(MeetingConclusion)
            messages = [
                {"role": "system", "content": CONCLUSION_SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": str(
                        {
                            "topic": topic,
                            "host_summary": summary,
                            "plan": [p.model_dump() for p in phases],
                            "recent": history[-8:],
                        }
                    ),
                },
            ]
            result = structured.invoke(messages)
            if isinstance(result, MeetingConclusion):
                return result
        except Exception as exc:  # provider failure → deterministic plan
            logger.warning("meeting conclusion builder failed: {}", exc)
        return conclusion_from_phases(topic, summary, phases)

    return build


def build_conclusion(
    history: list[dict[str, Any]],
    phases: list[MeetingPhase],
    topic: str,
    summary: str,
    *,
    builder: ConclusionFn | None = None,
) -> MeetingConclusion:
    """Build a conclusion from a meeting's history and execution plan."""
    build = builder or make_conclusion_builder()
    return build(history, phases, topic, summary)
