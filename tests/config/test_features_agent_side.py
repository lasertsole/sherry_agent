"""TDD contract tests for the agent-side feature config registry.

The registry (``config/features/agent_side/``) is the single source of truth
for agent-side feature tuneables; feature modules bind aliases to these fields.
These tests lock (a) every instance exposes exactly its TypedDict's keys,
(b) the frozen default values captured from the source modules, (c) the
env-aware ``_build_*`` helpers, and (d) collection field types.
"""

import pytest

from config.features.agent_side import (
    CONTEXT_ENGINE_HOOK,
    CONTEXT_GUARD,
    HEARTBEAT_STALENESS,
    HITL_DEFAULTS,
    ITERATION_BUDGET,
    LLM_CLIENT_DEFAULTS,
    LLM_RETRY,
    MAX_TOKENS_BOOST,
    MODEL_BACKEND,
    REASONING_BUDGET,
    REPETITION_GUARD,
    SUBAGENT_INFRA,
    SUMMARIZATION,
    TASKFLOW_INFRA,
    TODOLIST_INFRA,
    TOOLS_TIMEOUTS,
    TOKEN_ESTIMATION,
    TOOL_GUARDRAILS,
    ContextEngineHookConfig,
    ContextGuardConfig,
    HeartbeatStalenessConfig,
    HitlDefaultsConfig,
    IterationBudgetConfig,
    LlmClientDefaultsConfig,
    LlmRetryConfig,
    MaxTokensBoostConfig,
    ModelBackendConfig,
    ReasoningBudgetConfig,
    RepetitionGuardConfig,
    SubagentInfraConfig,
    SummarizationConfig,
    TaskFlowInfraConfig,
    TodoListInfraConfig,
    ToolsTimeoutsConfig,
    TokenEstimationConfig,
    ToolGuardrailsConfig,
)
from config.features.agent_side.max_tokens_boost import _build_max_tokens_boost
from config.features.agent_side.model_backend import _build_model_backend
from config.features.agent_side.reasoning_budget import _build_reasoning_budget

pytestmark = [pytest.mark.unit]

INSTANCE_TYPED_DICT_PAIRS = [
    (SUMMARIZATION, SummarizationConfig),
    (TOKEN_ESTIMATION, TokenEstimationConfig),
    (TOOL_GUARDRAILS, ToolGuardrailsConfig),
    (ITERATION_BUDGET, IterationBudgetConfig),
    (HEARTBEAT_STALENESS, HeartbeatStalenessConfig),
    (REPETITION_GUARD, RepetitionGuardConfig),
    (LLM_RETRY, LlmRetryConfig),
    (MAX_TOKENS_BOOST, MaxTokensBoostConfig),
    (SUBAGENT_INFRA, SubagentInfraConfig),
    (TASKFLOW_INFRA, TaskFlowInfraConfig),
    (TODOLIST_INFRA, TodoListInfraConfig),
    (TOOLS_TIMEOUTS, ToolsTimeoutsConfig),
    (HITL_DEFAULTS, HitlDefaultsConfig),
    (CONTEXT_ENGINE_HOOK, ContextEngineHookConfig),
    (CONTEXT_GUARD, ContextGuardConfig),
    (LLM_CLIENT_DEFAULTS, LlmClientDefaultsConfig),
    (REASONING_BUDGET, ReasoningBudgetConfig),
    (MODEL_BACKEND, ModelBackendConfig),
]

_PAIR_IDS = [typed_dict.__name__ for _, typed_dict in INSTANCE_TYPED_DICT_PAIRS]


@pytest.mark.parametrize(
    ("instance", "typed_dict"),
    INSTANCE_TYPED_DICT_PAIRS,
    ids=_PAIR_IDS,
)
def test_instance_keys_match_typed_dict_annotations(instance, typed_dict):
    assert set(instance.keys()) == set(typed_dict.__annotations__.keys())


# Spot defaults: at least three per dict, taken verbatim from the source files.
SPOT_DEFAULTS = [
    (SUMMARIZATION, "archive_threshold", 8_000),
    (SUMMARIZATION, "compression_trigger_ratio", 0.80),
    (SUMMARIZATION, "preemptive_truncate_max_chars", 2000),
    (SUMMARIZATION, "protected_tools", frozenset({"memory", "skill_view", "skill_list"})),
    (
        SUMMARIZATION,
        "auto_continue_prompt",
        "Continue if you have next steps, or stop and ask for clarification "
        "if you are unsure how to proceed.",
    ),
    (TOKEN_ESTIMATION, "chars_per_token", 4),
    (TOOL_GUARDRAILS, "warnings_enabled", True),
    (TOOL_GUARDRAILS, "hard_stop_enabled", False),
    (TOOL_GUARDRAILS, "exact_failure_block_after", 5),
    (TOOL_GUARDRAILS, "recovery_max_violations", 1),
    (ITERATION_BUDGET, "default_max_iterations", 50),
    (ITERATION_BUDGET, "main_agent_max_iterations", 90),
    (ITERATION_BUDGET, "worker_max_iterations", 60),
    (HEARTBEAT_STALENESS, "heartbeat_interval_minutes", 1),
    (HEARTBEAT_STALENESS, "stale_cycles_idle", 7),
    (HEARTBEAT_STALENESS, "stale_cycles_in_tool", 20),
    (REPETITION_GUARD, "min_content_length", 20),
    (REPETITION_GUARD, "internal_repeat_ratio", 0.6),
    (REPETITION_GUARD, "char_run_min", 8),
    (REPETITION_GUARD, "max_history", 30),
    (LLM_RETRY, "max_retries", 3),
    (LLM_RETRY, "base_delay", 2.0),
    (LLM_RETRY, "max_delay", 60.0),
    (LLM_RETRY, "stale_giveup_threshold", 5),
    (MAX_TOKENS_BOOST, "default_max_tokens", 8192),
    (MAX_TOKENS_BOOST, "max_cap", 32_768),
    (MAX_TOKENS_BOOST, "max_retries", 3),
    (SUBAGENT_INFRA, "registry_store_busy_timeout_ms", 5000),
    (SUBAGENT_INFRA, "registry_init_wait_timeout_s", 10.0),
    (SUBAGENT_INFRA, "delivery_compaction_retry_delays_ms", [1000, 2000, 4000, 8000]),
    (SUBAGENT_INFRA, "steer_abort_settle_timeout", 5.0),
    (SUBAGENT_INFRA, "followup_interval_multiplier", 2),
    (TASKFLOW_INFRA, "busy_timeout_ms", 5000),
    (TASKFLOW_INFRA, "persist_max_attempts", 3),
    (TASKFLOW_INFRA, "wait_all_min_poll_interval_seconds", 0.05),
    (TASKFLOW_INFRA, "wait_all_default_timeout_seconds", 300.0),
    (TODOLIST_INFRA, "store_busy_timeout_ms", 5000),
    (TODOLIST_INFRA, "stagnation_max_stagnation", 3),
    (TODOLIST_INFRA, "stagnation_base_cooldown_s", 2.0),
    (TODOLIST_INFRA, "stagnation_max_cooldown_s", 60.0),
    (TODOLIST_INFRA, "stagnation_failure_reset_window_s", 300),
    (TOOLS_TIMEOUTS, "web_search_timeout_seconds", 15),
    (TOOLS_TIMEOUTS, "terminal_timeout_seconds", 30),
    (TOOLS_TIMEOUTS, "python_repl_timeout_seconds", 30),
    (TOOLS_TIMEOUTS, "message_search_max_session_chars", 100_000),
    (TOOLS_TIMEOUTS, "skill_manage_max_skill_file_bytes", 1_048_576),
    (TOOLS_TIMEOUTS, "file_tools_search_max_context", 5),
    (HITL_DEFAULTS, "block_recurrence_limit", 3),
    (HITL_DEFAULTS, "default_timeout", 60),
    (HITL_DEFAULTS, "default_clarify_timeout", 3600),
    (HITL_DEFAULTS, "default_description_prefix", "Action requires human approval"),
    (CONTEXT_ENGINE_HOOK, "nudge_memory_threshold", 10),
    (CONTEXT_ENGINE_HOOK, "nudge_skill_threshold", 10),
    (CONTEXT_ENGINE_HOOK, "multimodal_temp_retention_days", 7),
    (CONTEXT_GUARD, "output_cut_ratio", 0.20),
    (CONTEXT_GUARD, "check_interval", 20),
    (LLM_CLIENT_DEFAULTS, "main_max_retries", 2),
    (LLM_CLIENT_DEFAULTS, "main_timeout", 120),
    (LLM_CLIENT_DEFAULTS, "main_stream_chunk_timeout", 60),
    (LLM_CLIENT_DEFAULTS, "aux_remote_max_tokens", 121072),
    (LLM_CLIENT_DEFAULTS, "local_n_gpu_layers", -1),
    (LLM_CLIENT_DEFAULTS, "ittt_remote_temperature", 0.8),
    (REASONING_BUDGET, "anthropic_default_thinking_budget", 2000),
]

_SPOT_IDS = [f"{key}={expected!r}" for _, key, expected in SPOT_DEFAULTS]


@pytest.mark.parametrize(
    ("instance", "key", "expected"),
    SPOT_DEFAULTS,
    ids=_SPOT_IDS,
)
def test_spot_defaults(instance, key, expected):
    assert instance[key] == expected


def test_max_tokens_boost_builder_reads_env_override():
    built = _build_max_tokens_boost({"MAIN_LLM_OUTPUT_MAX_TOKEN": "16384"})
    assert built["base_max_tokens"] == 16384
    assert built["default_max_tokens"] == 8192
    assert built["max_cap"] == 32_768
    assert built["max_retries"] == 3


def test_max_tokens_boost_builder_falls_back_to_default():
    built = _build_max_tokens_boost({"UNRELATED": "1"})
    assert built["base_max_tokens"] == 8192


def test_reasoning_budget_builder_reads_env_override():
    built = _build_reasoning_budget({"MAIN_LLM_THINKING_BUDGET": "2048"})
    assert built["non_anthropic_default_thinking_budget"] == 2048
    assert built["anthropic_default_thinking_budget"] == 2000


def test_reasoning_budget_builder_falls_back_to_default():
    built = _build_reasoning_budget({"UNRELATED": "1"})
    assert built["non_anthropic_default_thinking_budget"] == 4096


def test_model_backend_builder_reads_env_override():
    built = _build_model_backend({"ITTT_MODEL_LOCAL": "1", "VTTT_MODEL_LOCAL": "true"})
    assert built["ittt_model_local"] == 1
    assert built["vttt_model_local"] == 1


def test_model_backend_builder_falls_back_to_remote_default():
    built = _build_model_backend({"UNRELATED": "1"})
    assert built["ittt_model_local"] == 0
    assert built["vttt_model_local"] == 0


def test_frozenset_field_type():
    assert isinstance(SUMMARIZATION["protected_tools"], frozenset)
    assert SUMMARIZATION["protected_tools"] == frozenset({"memory", "skill_view", "skill_list"})


def test_list_field_types():
    transient = SUBAGENT_INFRA["delivery_transient_retry_delays_ms"]
    compaction = SUBAGENT_INFRA["delivery_compaction_retry_delays_ms"]
    assert isinstance(transient, list)
    assert isinstance(compaction, list)
    assert transient == [5000, 10000, 20000]
    assert compaction == [1000, 2000, 4000, 8000]
