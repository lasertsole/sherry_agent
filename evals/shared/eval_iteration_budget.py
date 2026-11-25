"""Eval-side main-agent iteration budget: pin with restore.

The product side keeps its two-level configuration (global default plus a
per-session override) and is NOT modified here. Evals have no server, so the
harness pins the process-wide default from ``SHERRY_EVAL_MAX_ITERATIONS``.
``EvalSandbox`` records and restores the original value so consecutive suites
in one interpreter cannot inherit a previous suite's budget.
"""

from __future__ import annotations

import os

_ENV_MAX_ITERATIONS = "SHERRY_EVAL_MAX_ITERATIONS"

#: Captured on the first real override, restored by ``EvalSandbox.restore()``.
#: Module-level (not sandbox-level) because suites pin BEFORE the sandbox
#: applies -- a sandbox-side snapshot would capture the already-pinned value
#: and leak it to the next suite in the same interpreter.
_ORIGINAL_BUDGET: int | None = None


def resolve_eval_iteration_budget() -> int:
    """Read the eval override from the environment.

    ``0`` (the default) means inherit the product default; a positive value
    replaces the main-agent ceiling. Invalid values degrade to ``0``.
    """
    raw = os.getenv(_ENV_MAX_ITERATIONS, "0").strip()
    try:
        return max(0, int(raw))
    except ValueError:
        return 0


def pin_eval_budget(max_iterations: int = 0) -> int:
    """Pin the main-agent iteration budget; return the effective ceiling.

    A non-positive value leaves the product default untouched and only reads
    it back, so suites that do not opt in behave exactly as before.
    """
    from config.features import ITERATION_BUDGET

    if max_iterations <= 0:
        return int(ITERATION_BUDGET["main_agent_max_iterations"])
    global _ORIGINAL_BUDGET
    if _ORIGINAL_BUDGET is None:
        _ORIGINAL_BUDGET = int(ITERATION_BUDGET["main_agent_max_iterations"])
    ITERATION_BUDGET["main_agent_max_iterations"] = max_iterations
    return max_iterations


def restore_eval_budget() -> None:
    """Undo the first pin; a no-op when no override was applied."""
    global _ORIGINAL_BUDGET
    if _ORIGINAL_BUDGET is not None:
        from config.features import ITERATION_BUDGET

        ITERATION_BUDGET["main_agent_max_iterations"] = _ORIGINAL_BUDGET
        _ORIGINAL_BUDGET = None


def eval_main_max_iterations() -> int:
    """The main agent's effective model-call ceiling in this process."""
    from config.features import ITERATION_BUDGET

    return int(ITERATION_BUDGET["main_agent_max_iterations"])
