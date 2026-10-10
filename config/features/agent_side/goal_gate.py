"""TaskFlow Gate E: the LLM goal judge that gates ``taskflow_finish``."""

from typing import TypedDict


class GoalGateConfig(TypedDict):
    """TaskFlow Gate E: the LLM goal judge that gates ``taskflow_finish``."""

    #: Master switch (the gate is a model call, so it can be turned off).
    enabled: bool
    #: Characters of each step result handed to the judge (per result).
    max_result_chars: int


GOAL_GATE: GoalGateConfig = {
    "enabled": True,
    "max_result_chars": 4000,
}
