"""Subagent completion-judge / goal-loop configuration.

The completion judge is an auxiliary-LLM discriminator that reviews a spawned
subagent's latest response against its task and returns DONE / CONTINUE. A
CONTINUE verdict injects the judge's continuation prompt and the subagent runs
another turn (goal loop) up to ``goal_max_turns`` turns. It is fail-open: an
unreachable judge degrades to DONE, so it can never trap a subagent in the loop.

``enabled`` is the master switch: with it
off, neither the goal loop nor a single judge call happens, whatever
``goal_max_turns`` a caller passes — ``goal_max_turns=1`` was the old implicit
workaround and a caller could raise it back on.

One judge call is skipped even while enabled: a first turn that produced a
result AND left at least one passing, non-stale evidence row in the child's
ledger. The judge could only restate what the ledger already proves, so a
simple spawn ("read this file") no longer pays a judge call for it.
"""

from typing import TypedDict


class CompletionJudgeConfig(TypedDict):
    """Tuneables for the subagent completion judge and its goal loop."""

    enabled: bool
    """Master switch for the goal loop and every judge call it would make."""

    goal_max_turns: int
    """Goal-loop turn budget (including the first turn) before it is exhausted."""


COMPLETION_JUDGE: CompletionJudgeConfig = {
    "enabled": True,
    "goal_max_turns": 5,
}
