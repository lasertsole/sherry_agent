"""Failure-pathology thresholds for the tool guardrails middleware."""

from typing import TypedDict


class ToolGuardrailsConfig(TypedDict):
    """Failure-pathology thresholds for the tool guardrails middleware."""

    warnings_enabled: bool
    hard_stop_enabled: bool
    exact_failure_warn_after: int
    exact_failure_block_after: int
    same_tool_failure_warn_after: int
    same_tool_failure_halt_after: int
    no_progress_warn_after: int
    no_progress_block_after: int
    ping_pong_warn_after: int
    ping_pong_block_after: int
    arg_churn_min_calls_per_variant: int
    arg_churn_warn_after: int
    arg_churn_block_after: int
    recovery_mode_enabled: bool
    recovery_max_violations: int


TOOL_GUARDRAILS: ToolGuardrailsConfig = {
    "warnings_enabled": True,
    "hard_stop_enabled": False,
    "exact_failure_warn_after": 2,
    "exact_failure_block_after": 5,
    "same_tool_failure_warn_after": 3,
    "same_tool_failure_halt_after": 8,
    "no_progress_warn_after": 2,
    "no_progress_block_after": 5,
    "ping_pong_warn_after": 4,
    "ping_pong_block_after": 6,
    "arg_churn_min_calls_per_variant": 3,
    "arg_churn_warn_after": 3,
    "arg_churn_block_after": 5,
    "recovery_mode_enabled": True,
    "recovery_max_violations": 1,
}
