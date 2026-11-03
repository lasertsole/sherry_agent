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
    #: Signature-level repetition (same tool + same stable args, regardless of
    #: result status). The first ``streak_max_warnings_per_turn`` streaks WARN
    #: and reset; once that budget is spent the exact signature is BLOCKED for
    #: the rest of the turn (other arguments and tools stay available). A tool
    #: may override the threshold with its own ``metadata["streak_warn_after"]``
    #: (terminal declares 5 because re-running the same test command can be a
    #: legitimate iteration).
    streak_warn_after: int
    streak_max_warnings_per_turn: int
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
    "streak_warn_after": 3,
    "streak_max_warnings_per_turn": 3,
    "recovery_mode_enabled": True,
    "recovery_max_violations": 1,
}
