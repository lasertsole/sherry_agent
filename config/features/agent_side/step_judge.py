"""TaskFlow step-judge configuration.

The step judge is an auxiliary-LLM discriminator that reviews a step's
subagent result against its ``validation_criteria`` and returns
PASS / RETRY / BLOCK. It is fail-open: a disabled or unreachable judge never
blocks progress.
"""

from typing import TypedDict


class StepJudgeConfig(TypedDict):
    """Tuneables for the TaskFlow step judge."""

    enabled: bool
    """Global switch. False skips every judge call (fail-open to PASS)."""
    max_retries: int
    """Maximum RETRY verdicts; reuses the step's existing ``retry_count`` budget."""
    max_result_chars: int
    """Result-text truncation length handed to the judge prompt."""
    evidence_aware: bool
    """Whether the judge prompt includes the verification-evidence summary."""


STEP_JUDGE: StepJudgeConfig = {
    "enabled": True,
    "max_retries": 2,
    "max_result_chars": 8000,
    "evidence_aware": True,
}
