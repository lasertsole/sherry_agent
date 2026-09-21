"""Subagent completion-judge / goal-loop configuration.

The completion judge is an auxiliary-LLM discriminator that reviews a spawned
subagent's latest response against its task and returns DONE / CONTINUE. A
CONTINUE verdict injects the judge's continuation prompt and the subagent runs
another turn (goal loop) up to ``goal_max_turns`` turns. It is fail-open: a
disabled or unreachable judge degrades to DONE, so it can never trap a subagent
in the loop. Both switches are opt-in — spawning stays single-turn by default.
"""

from typing import TypedDict


class CompletionJudgeConfig(TypedDict):
    """Tuneables for the subagent completion judge and its goal loop."""

    enabled: bool
    """Global switch. False disables the goal loop (subagent runs single-turn)."""
    goal_max_turns: int
    """Goal-loop turn budget (including the first turn) before it is exhausted."""


COMPLETION_JUDGE: CompletionJudgeConfig = {
    "enabled": False,
    "goal_max_turns": 5,
}
