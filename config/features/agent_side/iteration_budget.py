"""Per-turn model+tool call budgets."""

from typing import TypedDict


class IterationBudgetConfig(TypedDict):
    """Per-turn model+tool call budgets and their registered values."""

    default_max_iterations: int
    main_agent_max_iterations: int
    worker_max_iterations: int


ITERATION_BUDGET: IterationBudgetConfig = {
    "default_max_iterations": 50,
    "main_agent_max_iterations": 90,
    "worker_max_iterations": 60,
}
