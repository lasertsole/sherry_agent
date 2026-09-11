"""Agent-side feature configuration registry.

Purpose
-------
One ``TypedDict`` per agent-side feature holding the frozen default values for
every tuneable that the feature currently declares as a module-level constant,
a dataclass field default, or an environment-sourced value. This module is the
**single source of truth**; feature modules bind aliases to these fields instead
of declaring their own copies.

The module is deliberately dependency-free: it imports only the standard
library, so it can be consumed from anywhere (including ``models/``) without a
circular import back into ``agent/`` or ``models/``.

Environment-sourced fields are read when the module-level instances are built
(import time), preserving today's import-time binding semantics. The private
``_build_*`` helpers accept an optional mapping so tests can inject values.
"""

import os
from collections.abc import Mapping
from typing import TypedDict

type FeatureConfig = Mapping[str, object]

_TRUE_ENV_VALUES = frozenset({"1", "true", "yes", "on"})
_FALSE_ENV_VALUES = frozenset({"0", "false", "no", "off", ""})


def _env_int(name: str, default: int, env: Mapping[str, str]) -> int:
    """Read ``name`` from ``env`` as an int, falling back to ``default``.

    Accepts the boolean-like spellings the model-local flags use (``"true"`` /
    ``"false"``) so an import-time build never raises on a non-numeric value.
    """
    raw = env.get(name)
    if raw is None:
        return default
    text = raw.strip().lower()
    if text in _TRUE_ENV_VALUES:
        return 1
    if text in _FALSE_ENV_VALUES:
        return 0
    try:
        return int(text)
    except ValueError:
        return default


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


SUMMARIZATION: FeatureConfig = {
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


class TokenEstimationConfig(TypedDict):
    """Token-estimation constant shared by truncation helpers."""

    chars_per_token: int


TOKEN_ESTIMATION: FeatureConfig = {
    "chars_per_token": 4,
}


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


TOOL_GUARDRAILS: FeatureConfig = {
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


class IterationBudgetConfig(TypedDict):
    """Per-turn model+tool call budgets and their registered values."""

    default_max_iterations: int
    main_agent_max_iterations: int
    worker_max_iterations: int


ITERATION_BUDGET: FeatureConfig = {
    "default_max_iterations": 50,
    "main_agent_max_iterations": 90,
    "worker_max_iterations": 60,
}


class HeartbeatStalenessConfig(TypedDict):
    """Heartbeat watchdog cadence and stale-cycle kill thresholds."""

    heartbeat_interval_minutes: int
    stale_cycles_idle: int
    stale_cycles_in_tool: int


HEARTBEAT_STALENESS: FeatureConfig = {
    "heartbeat_interval_minutes": 1,
    "stale_cycles_idle": 7,
    "stale_cycles_in_tool": 20,
}


class RepetitionGuardConfig(TypedDict):
    """Output-repetition detection thresholds (cross-call and internal)."""

    min_content_length: int
    min_crosscall_length: int
    max_identical_outputs: int
    warn_after: int
    internal_repeat_ratio: float
    internal_min_lines: int
    char_run_min: int
    tail_chars: int
    phrase_min_repeats: int
    phrase_max_phrase_len: int
    max_history: int


REPETITION_GUARD: FeatureConfig = {
    "min_content_length": 20,
    "min_crosscall_length": 1,
    "max_identical_outputs": 3,
    "warn_after": 2,
    "internal_repeat_ratio": 0.6,
    "internal_min_lines": 6,
    "char_run_min": 8,
    "tail_chars": 500,
    "phrase_min_repeats": 5,
    "phrase_max_phrase_len": 10,
    "max_history": 30,
}


class LlmRetryConfig(TypedDict):
    """Bounded retry / jittered-backoff defaults for the LLM retry middleware."""

    max_retries: int
    base_delay: float
    max_delay: float
    jitter: float
    stale_giveup_threshold: int


LLM_RETRY: FeatureConfig = {
    "max_retries": 3,
    "base_delay": 2.0,
    "max_delay": 60.0,
    "jitter": 0.3,
    "stale_giveup_threshold": 5,
}


class MaxTokensBoostConfig(TypedDict):
    """Tool-call truncation boost base/cap/retry settings."""

    base_max_tokens: int
    default_max_tokens: int
    max_cap: int
    max_retries: int


def _build_max_tokens_boost(env: Mapping[str, str] | None = None) -> MaxTokensBoostConfig:
    """Build the max-tokens boost config, reading the env at call time."""
    source = env or os.environ
    return {
        "base_max_tokens": _env_int("MAIN_LLM_OUTPUT_MAX_TOKEN", 8192, source),
        "default_max_tokens": 8192,
        "max_cap": 32_768,
        "max_retries": 3,
    }


MAX_TOKENS_BOOST: FeatureConfig = _build_max_tokens_boost()


class SubagentInfraConfig(TypedDict):
    """Scattered subagent infrastructure tunables (outside SubagentConfig)."""

    registry_store_busy_timeout_ms: int
    registry_init_wait_timeout_s: float
    pending_injections_busy_timeout_ms: int
    sessions_yield_default_timeout_seconds: float
    sessions_send_timeout_seconds: float
    thread_binding_idle_timeout_ms: int
    thread_binding_max_age_ms: int
    delivery_mirror_max: int
    delivery_transient_retry_delays_ms: list[int]
    delivery_compaction_retry_delays_ms: list[int]
    orphan_wedged_age_seconds: int
    orphan_max_recovery_attempts: int
    orphan_max_terminal_finalize_attempts: int
    helpers_frozen_result_cap_bytes: int
    helpers_announce_retry_base_ms: int
    helpers_announce_retry_cap_ms: int
    capture_max_wait_ms: int
    capture_retry_interval_ms: int
    steer_abort_settle_timeout: float
    followup_interval_multiplier: int


SUBAGENT_INFRA: FeatureConfig = {
    "registry_store_busy_timeout_ms": 5000,
    "registry_init_wait_timeout_s": 10.0,
    "pending_injections_busy_timeout_ms": 5000,
    "sessions_yield_default_timeout_seconds": 300.0,
    "sessions_send_timeout_seconds": 30.0,
    "thread_binding_idle_timeout_ms": 300000,
    "thread_binding_max_age_ms": 86400000,
    "delivery_mirror_max": 5000,
    "delivery_transient_retry_delays_ms": [5000, 10000, 20000],
    "delivery_compaction_retry_delays_ms": [1000, 2000, 4000, 8000],
    "orphan_wedged_age_seconds": 86400,
    "orphan_max_recovery_attempts": 3,
    "orphan_max_terminal_finalize_attempts": 3,
    "helpers_frozen_result_cap_bytes": 24000,
    "helpers_announce_retry_base_ms": 1000,
    "helpers_announce_retry_cap_ms": 8000,
    "capture_max_wait_ms": 5000,
    "capture_retry_interval_ms": 500,
    "steer_abort_settle_timeout": 5.0,
    "followup_interval_multiplier": 2,
}


class TaskFlowInfraConfig(TypedDict):
    """TaskFlow store timeouts, persist retries, and wait_all polling."""

    busy_timeout_ms: int
    init_wait_timeout_s: float
    persist_max_attempts: int
    wait_all_min_poll_interval_seconds: float
    wait_all_default_timeout_seconds: float
    wait_all_default_poll_interval_seconds: float


TASKFLOW_INFRA: FeatureConfig = {
    "busy_timeout_ms": 5000,
    "init_wait_timeout_s": 10.0,
    "persist_max_attempts": 3,
    "wait_all_min_poll_interval_seconds": 0.05,
    "wait_all_default_timeout_seconds": 300.0,
    "wait_all_default_poll_interval_seconds": 0.5,
}


class TodoListInfraConfig(TypedDict):
    """TodoList store timeouts and stagnation-tracker thresholds."""

    store_busy_timeout_ms: int
    store_init_wait_timeout_s: float
    stagnation_max_stagnation: int
    stagnation_base_cooldown_s: float
    stagnation_max_cooldown_s: float
    stagnation_failure_reset_window_s: int
    stagnation_max_recovery_attempts: int


TODOLIST_INFRA: FeatureConfig = {
    "store_busy_timeout_ms": 5000,
    "store_init_wait_timeout_s": 10.0,
    "stagnation_max_stagnation": 3,
    "stagnation_base_cooldown_s": 2.0,
    "stagnation_max_cooldown_s": 60.0,
    "stagnation_failure_reset_window_s": 300,
    "stagnation_max_recovery_attempts": 2,
}


class ToolsTimeoutsConfig(TypedDict):
    """Per-tool timeouts, retry budgets, and schema size limits."""

    web_search_timeout_seconds: int
    web_search_retry_backoff_min_s: int
    web_search_retry_backoff_max_s: int
    web_search_retry_max_attempts: int
    terminal_timeout_seconds: int
    python_repl_timeout_seconds: int
    sandbox_bwrap_probe_timeout_seconds: int
    message_search_max_session_chars: int
    skill_view_max_name_length: int
    skill_view_max_description_length: int
    skill_manage_max_name_length: int
    skill_manage_max_description_length: int
    skill_manage_max_skill_content_chars: int
    skill_manage_umbrella_skill_char_target: int
    skill_manage_max_skill_file_bytes: int
    file_tools_read_default_limit: int
    file_tools_read_max_limit: int
    file_tools_search_default_limit: int
    file_tools_search_max_limit: int
    file_tools_search_max_context: int
    question_option_max_length: int
    question_min_length: int
    question_custom_max_length: int


TOOLS_TIMEOUTS: FeatureConfig = {
    "web_search_timeout_seconds": 15,
    "web_search_retry_backoff_min_s": 5,
    "web_search_retry_backoff_max_s": 45,
    "web_search_retry_max_attempts": 3,
    "terminal_timeout_seconds": 30,
    "python_repl_timeout_seconds": 30,
    "sandbox_bwrap_probe_timeout_seconds": 3,
    "message_search_max_session_chars": 100_000,
    "skill_view_max_name_length": 64,
    "skill_view_max_description_length": 1024,
    "skill_manage_max_name_length": 64,
    "skill_manage_max_description_length": 1024,
    "skill_manage_max_skill_content_chars": 100_000,
    "skill_manage_umbrella_skill_char_target": 15_000,
    "skill_manage_max_skill_file_bytes": 1_048_576,
    "file_tools_read_default_limit": 500,
    "file_tools_read_max_limit": 2000,
    "file_tools_search_default_limit": 50,
    "file_tools_search_max_limit": 200,
    "file_tools_search_max_context": 5,
    "question_option_max_length": 30,
    "question_min_length": 2,
    "question_custom_max_length": 6,
}


class HitlDefaultsConfig(TypedDict):
    """Human-in-the-loop approval defaults."""

    block_recurrence_limit: int
    default_timeout: int
    default_clarify_timeout: int
    default_kanban_recurrence_limit: int
    default_mcp_reload_confirm: bool
    default_destructive_slash_confirm: bool
    default_description_prefix: str


HITL_DEFAULTS: FeatureConfig = {
    "block_recurrence_limit": 3,
    "default_timeout": 60,
    "default_clarify_timeout": 3600,
    "default_kanban_recurrence_limit": 3,
    "default_mcp_reload_confirm": True,
    "default_destructive_slash_confirm": True,
    "default_description_prefix": "Action requires human approval",
}


class ContextEngineHookConfig(TypedDict):
    """Context-engine nudge thresholds and multimodal temp retention."""

    nudge_memory_threshold: int
    nudge_skill_threshold: int
    multimodal_temp_retention_days: int


CONTEXT_ENGINE_HOOK: FeatureConfig = {
    "nudge_memory_threshold": 10,
    "nudge_skill_threshold": 10,
    "multimodal_temp_retention_days": 7,
}


class ContextGuardConfig(TypedDict):
    """Mid-stream output budget guard settings."""

    output_cut_ratio: float
    check_interval: int


CONTEXT_GUARD: FeatureConfig = {
    "output_cut_ratio": 0.20,
    "check_interval": 20,
}


class LlmClientDefaultsConfig(TypedDict):
    """LLM client construction defaults across main/aux/reasoner/local models."""

    main_max_retries: int
    main_timeout: int
    main_stream_chunk_timeout: int
    fallback_max_retries: int
    fallback_timeout: int
    aux_max_retries: int
    aux_remote_max_tokens: int
    reasoner_max_retries: int
    reasoner_max_tokens_cap: int
    local_n_ctx: int
    local_temperature: float
    local_max_tokens: int
    local_n_gpu_layers: int
    ittt_remote_temperature: float
    ittt_remote_max_retries: int
    vttt_remote_temperature: float
    vttt_remote_max_retries: int


LLM_CLIENT_DEFAULTS: FeatureConfig = {
    "main_max_retries": 2,
    "main_timeout": 120,
    "main_stream_chunk_timeout": 60,
    "fallback_max_retries": 2,
    "fallback_timeout": 120,
    "aux_max_retries": 2,
    "aux_remote_max_tokens": 121072,
    "reasoner_max_retries": 2,
    "reasoner_max_tokens_cap": 65536,
    "local_n_ctx": 4096,
    "local_temperature": 0.0,
    "local_max_tokens": 4096,
    "local_n_gpu_layers": -1,
    "ittt_remote_temperature": 0.8,
    "ittt_remote_max_retries": 2,
    "vttt_remote_temperature": 0.8,
    "vttt_remote_max_retries": 2,
}


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


REASONING_BUDGET: FeatureConfig = _build_reasoning_budget()


class ModelBackendConfig(TypedDict):
    """Local-vs-remote backend flags for the multimodal understanding models."""

    ittt_model_local: int
    vttt_model_local: int


def _build_model_backend(env: Mapping[str, str] | None = None) -> ModelBackendConfig:
    """Build the model-backend flags, reading the env at call time."""
    source = env or os.environ
    return {
        "ittt_model_local": _env_int("ITTT_MODEL_LOCAL", 0, source),
        "vttt_model_local": _env_int("VTTT_MODEL_LOCAL", 0, source),
    }


MODEL_BACKEND: FeatureConfig = _build_model_backend()
