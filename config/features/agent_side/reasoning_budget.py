"""Reasoning/thinking token budgets for Anthropic and non-Anthropic providers."""

import os
from collections.abc import Mapping
from typing import TypedDict

from config.features._env import _env_int


class ReasoningBudgetConfig(TypedDict):
    """Reasoning/thinking token budgets for Anthropic and non-Anthropic providers."""

    anthropic_default_thinking_budget: int
    non_anthropic_default_thinking_budget: int
    #: Per-LEVEL headroom for the shared-``max_tokens`` providers: a plain ON
    #: call uses the default above (byte-compatible), while an explicit level
    #: scales the headroom with the effort the user picked.
    thinking_budgets: dict[str, int]


def _build_reasoning_budget(env: Mapping[str, str] | None = None) -> ReasoningBudgetConfig:
    """Build the reasoning-budget config, reading the env at call time."""
    source = env or os.environ
    return {
        "anthropic_default_thinking_budget": 2000,
        "non_anthropic_default_thinking_budget": _env_int("MAIN_LLM_THINKING_BUDGET", 4096, source),
        # The ladder keeps the ON default (4096, "high") in the middle so an
        # existing session's cost does not change when the table is introduced.
        "thinking_budgets": {
            "minimal": 1024,
            "low": 2048,
            "medium": 3072,
            "high": 4096,
            "xhigh": 5120,
            "max": 6144,
        },
    }


REASONING_BUDGET: ReasoningBudgetConfig = _build_reasoning_budget()
