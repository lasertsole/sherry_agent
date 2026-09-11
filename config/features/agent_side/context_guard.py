"""Mid-stream output budget guard settings."""

from typing import TypedDict


class ContextGuardConfig(TypedDict):
    """Mid-stream output budget guard settings."""

    output_cut_ratio: float
    check_interval: int


CONTEXT_GUARD: ContextGuardConfig = {
    "output_cut_ratio": 0.20,
    "check_interval": 20,
}
