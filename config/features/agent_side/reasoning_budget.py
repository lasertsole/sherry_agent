"""Reasoning/thinking token budgets for Anthropic and non-Anthropic providers."""

import os
from collections.abc import Mapping
from typing import TypedDict

from config.features._env import _env_int


class ReasoningBudgetConfig(TypedDict):
    """Reasoning/thinking token budgets for Anthropic and non-Anthropic providers."""

    anthropic_default_thinking_budget: int
    non_anthropic_default_thinking_budget: int


def _build_reasoning_budget(env: Mapping[str, str] | None = None) -> ReasoningBudgetConfig:
    """Build the reasoning-budget config, reading the env at call time."""
    source = env or os.environ
    return {
        "anthropic_default_thinking_budget": 2000,
        "non_anthropic_default_thinking_budget": _env_int("MAIN_LLM_THINKING_BUDGET", 4096, source),
    }


REASONING_BUDGET: ReasoningBudgetConfig = _build_reasoning_budget()
