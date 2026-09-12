"""Per-model token pricing and the token-budget warning threshold (GAP-3).

Prices are USD per 1,000,000 tokens and are estimates used only for budget
warnings, never for billing. A model absent from the table falls back to the
``_default`` entry at lookup time.
"""

from typing import TypedDict


class ModelPricingConfig(TypedDict):
    """Model price table plus the budget warning threshold (fraction of budget)."""

    model_pricing_per_m_tokens: dict[str, dict[str, float]]
    budget_warn_threshold: float


MODEL_PRICING: ModelPricingConfig = {
    "model_pricing_per_m_tokens": {
        "glm-5": {"input": 0.5, "output": 1.5},
        "deepseek-chat": {"input": 0.14, "output": 0.28},
        "kimi-latest": {"input": 0.55, "output": 2.19},
        "_default": {"input": 1.0, "output": 3.0},
    },
    "budget_warn_threshold": 0.80,
}
