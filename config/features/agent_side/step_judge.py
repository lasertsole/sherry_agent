"""TaskFlow step-judge configuration.

The step judge is an auxiliary-LLM discriminator that reviews a step's
subagent result against its ``validation_criteria`` and returns
PASS / RETRY / BLOCK. It runs for every criteria-bearing step.

When the judge ITSELF fails (provider/connection error, unparseable output) it
returns ``UNKNOWN`` rather than a real verdict. ``on_judge_error`` decides how
the consumer treats that: ``"block"`` (default) keeps the step non-terminal so
an unverified result cannot be marked ``done`` and unlock dependents;
``"pass"`` restores the historical fail-open behaviour. An ``UNKNOWN`` from a
judge outage is never confused with a model-issued PASS.
"""

from typing import Literal, TypedDict


class StepJudgeConfig(TypedDict):
    """Tuneables for the TaskFlow step judge."""

    max_retries: int
    """Maximum RETRY verdicts; reuses the step's existing ``retry_count`` budget."""
    max_result_chars: int
    """Result-text truncation length handed to the judge prompt."""
    evidence_aware: bool
    """Whether the judge prompt includes the verification-evidence summary."""
    on_judge_error: Literal["block", "pass"]
    """How to treat ``UNKNOWN`` (judge itself failed): block (safe default) or pass (legacy fail-open)."""


STEP_JUDGE: StepJudgeConfig = {
    "max_retries": 2,
    "max_result_chars": 8000,
    "evidence_aware": True,
    "on_judge_error": "block",
}
