"""Summarization / context-compaction thresholds for the agent pipeline."""

from typing import TypedDict


class SummarizationConfig(TypedDict):
    """Summarization / context-compaction thresholds (single source of truth for the summarization pipeline)."""

    archive_threshold: int
    memory_threshold: int
    compress_ratio: float
    preemptive_truncate_ratio: float
    compression_trigger_ratio: float
    compression_reserve_tokens: int
    min_preserve_tokens: int
    max_preserve_tokens: int
    preserve_ratio: float
    prune_protect_tokens: int
    prune_min_reduction_tokens: int
    target_truncate_ratio: float
    min_output_chars_to_truncate: int
    max_tool_output_chars: int
    min_args_chars_to_truncate: int
    max_tool_args_chars: int
    aggressive_truncate_chars: int
    summary_trim_tokens: int
    summary_total_max_chars: int
    content_head_ratio: float
    content_tail_ratio: float
    degradation_monitor_count: int
    degradation_no_text_threshold: int
    max_recovery_attempts: int
    max_total_compression_attempts: int
    ineffective_threshold: int
    min_effectiveness_pct: float
    protected_tools: frozenset[str]
    last_turn_ratio_threshold: float
    completed_max_items: int
    key_decisions_max_items: int
    critical_context_max_items: int
    file_ops_list_max_chars: int
    file_ops_section_max_chars: int
    latest_user_request_max_chars: int
    auto_continue_prompt: str
    max_overflow_retries: int
    max_compress_attempts_per_turn: int
    compaction_cooldown_rounds: int
    prune_ttl_seconds: int
    truncate_budget_ratio: float
    min_tool_result_tokens_to_truncate: int
    truncatable_recent_skip: int
    ttl_registry_max_entries: int
    preemptive_truncate_max_chars: int


SUMMARIZATION: SummarizationConfig = {
    "archive_threshold": 8_000,
    "memory_threshold": 10_000,
    "compress_ratio": 0.5,
    "preemptive_truncate_ratio": 0.70,
    "compression_trigger_ratio": 0.80,
    "compression_reserve_tokens": 16_000,
    "min_preserve_tokens": 2_000,
    "max_preserve_tokens": 15_000,
    "preserve_ratio": 0.25,
    "prune_protect_tokens": 40_000,
    "prune_min_reduction_tokens": 5_000,
    "target_truncate_ratio": 0.5,
    "min_output_chars_to_truncate": 500,
    "max_tool_output_chars": 2_000,
    "min_args_chars_to_truncate": 500,
    "max_tool_args_chars": 2_000,
    "aggressive_truncate_chars": 1_000,
    "summary_trim_tokens": 12_000,
    "summary_total_max_chars": 16_000,
    "content_head_ratio": 0.3,
    "content_tail_ratio": 0.3,
    "degradation_monitor_count": 5,
    "degradation_no_text_threshold": 3,
    "max_recovery_attempts": 2,
    "max_total_compression_attempts": 5,
    "ineffective_threshold": 2,
    "min_effectiveness_pct": 0.05,
    "protected_tools": frozenset({"memory", "skill_view", "skill_list"}),
    "last_turn_ratio_threshold": 0.5,
    "completed_max_items": 5,
    "key_decisions_max_items": 5,
    "critical_context_max_items": 3,
    "file_ops_list_max_chars": 900,
    "file_ops_section_max_chars": 2_000,
    "latest_user_request_max_chars": 800,
    "auto_continue_prompt": (
        "Continue if you have next steps, or stop and ask for clarification "
        "if you are unsure how to proceed."
    ),
    "max_overflow_retries": 3,
    "max_compress_attempts_per_turn": 3,
    "compaction_cooldown_rounds": 3,
    "prune_ttl_seconds": 300,
    "truncate_budget_ratio": 0.6,
    "min_tool_result_tokens_to_truncate": 200,
    "truncatable_recent_skip": 6,
    "ttl_registry_max_entries": 512,
    "preemptive_truncate_max_chars": 2000,
}
