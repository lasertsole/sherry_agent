"""Subagent completion-judge / goal-loop configuration.

The completion judge is an auxiliary-LLM discriminator that reviews a spawned
subagent's latest response against its task and returns DONE / CONTINUE. A
CONTINUE verdict injects the judge's continuation prompt and the subagent runs
another turn (goal loop) up to ``goal_max_turns`` turns. It is fail-open: an
unreachable judge degrades to DONE, so it can never trap a subagent in the loop.
The goal loop runs for every spawn; ``goal_max_turns`` is its budget.
"""

from typing import TypedDict


class CompletionJudgeConfig(TypedDict):
    """Tuneables for the subagent completion judge and its goal loop."""

    goal_max_turns: int
    """Goal-loop turn budget (including the first turn) before it is exhausted."""


COMPLETION_JUDGE: CompletionJudgeConfig = {
    "goal_max_turns": 5,
}
