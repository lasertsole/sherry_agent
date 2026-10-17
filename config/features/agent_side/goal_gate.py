"""TaskFlow Gate E: the LLM goal judge that gates ``taskflow_finish``."""

from typing import TypedDict


class GoalGateConfig(TypedDict):
    """TaskFlow Gate E: the LLM goal judge that gates ``taskflow_finish``."""

    #: Master switch (the gate is a model call, so it can be turned off).
    enabled: bool
    #: Characters of each step result handed to the judge (per result).
    max_result_chars: int
    #: Minimum flow size for the judge to run at all. Below it the gate is
    #: skipped: a one-or-two-step flow is already covered by the child-level
    #: CompletionJudge, and a second LLM verdict on the same work is the
    #: overlap between the flow-level gate and the child-level CompletionJudge.
    min_steps: int


GOAL_GATE: GoalGateConfig = {
    "enabled": True,
    "max_result_chars": 4000,
    "min_steps": 3,
}
