"""Per-turn model-call budgets (a tool-call pair costs one unit)."""

from typing import TypedDict


class IterationBudgetConfig(TypedDict):
    """Per-turn model-call budgets and their registered values.

    The counter charges one unit per model call (the AIMessage-producing
    iteration). ToolMessage results, user messages and system reminders never
    charge it, so a tool-call pair is not double-counted.
    """

    default_max_iterations: int
    main_agent_max_iterations: int
    worker_max_iterations: int
    executor_max_iterations: int


ITERATION_BUDGET: IterationBudgetConfig = {
    "default_max_iterations": 50,
    "main_agent_max_iterations": 180,
    "worker_max_iterations": 60,
    # The executor is the write-capable worker: it needs more room than a plain
    # researcher/reviewer's 60-call ceiling, but stays below the main agent's
    # 180 so a runaway child cannot outlive its parent's turn.
    "executor_max_iterations": 90,
}
