"""TDD contract tests: config.num summarization constant contract.

The ``from config.num import (...)`` block below is the authoritative import
contract for the summarization middleware
(``agent/middlewares/summarization.py``); expected values match
``config/num.py``, whose semantics are documented in
``docs/harness/summarization/README.md``.

Also guards the pre-existing constants of the old config/num.py baseline
(ARCHIVE_THRESHOLD, MEMORY_THRESHOLD, COMPRESS_RATIO, BUS_QUEUE_MAXSIZE)
and CHARS_PER_TOKEN (consumed by pub_func/message/estimate_msg_tokens.py).
"""

import importlib

from config.num import (
    PREEMPTIVE_TRUNCATE_RATIO,
    COMPRESSION_TRIGGER_RATIO,
    MIN_PRESERVE_TOKENS,
    MAX_PRESERVE_TOKENS,
    PRESERVE_RATIO,
    PRUNE_PROTECT_TOKENS,
    PRUNE_MIN_REDUCTION_TOKENS,
    TARGET_TRUNCATE_RATIO,
    MIN_OUTPUT_CHARS_TO_TRUNCATE,
    MAX_TOOL_OUTPUT_CHARS,
    AGGRESSIVE_TRUNCATE_CHARS,
    SUMMARY_TRIM_TOKENS,
    SUMMARY_TOTAL_MAX_CHARS,
    CONTENT_HEAD_RATIO,
    CONTENT_TAIL_RATIO,
    DEGRADATION_NO_TEXT_THRESHOLD,
    MAX_RECOVERY_ATTEMPTS,
    MAX_TOTAL_COMPRESSION_ATTEMPTS,
    INEFFECTIVE_THRESHOLD,
    MIN_EFFECTIVENESS_PCT,
    PROTECTED_TOOLS,
    LAST_TURN_RATIO_THRESHOLD,
    COMPLETED_MAX_ITEMS,
    KEY_DECISIONS_MAX_ITEMS,
    CRITICAL_CONTEXT_MAX_ITEMS,
    FILE_OPS_LIST_MAX_CHARS,
    LATEST_USER_REQUEST_MAX_CHARS,
    AUTO_CONTINUE_PROMPT,
    MAX_OVERFLOW_RETRIES,
    MAX_COMPRESS_ATTEMPTS_PER_TURN,
    COMPACTION_COOLDOWN_ROUNDS,
    PRUNE_TTL_SECONDS,
    TRUNCATE_BUDGET_RATIO,
    MIN_TOOL_RESULT_TOKENS_TO_TRUNCATE,
    TRUNCATABLE_RECENT_SKIP,
    TTL_REGISTRY_MAX_ENTRIES,
)

CONTRACT_NAMES = [
    "PREEMPTIVE_TRUNCATE_RATIO",
    "COMPRESSION_TRIGGER_RATIO",
    "MIN_PRESERVE_TOKENS",
    "MAX_PRESERVE_TOKENS",
    "PRESERVE_RATIO",
    "PRUNE_PROTECT_TOKENS",
    "PRUNE_MIN_REDUCTION_TOKENS",
    "TARGET_TRUNCATE_RATIO",
    "MIN_OUTPUT_CHARS_TO_TRUNCATE",
    "MAX_TOOL_OUTPUT_CHARS",
    "AGGRESSIVE_TRUNCATE_CHARS",
    "SUMMARY_TRIM_TOKENS",
    "SUMMARY_TOTAL_MAX_CHARS",
    "CONTENT_HEAD_RATIO",
    "CONTENT_TAIL_RATIO",
    "DEGRADATION_NO_TEXT_THRESHOLD",
    "MAX_RECOVERY_ATTEMPTS",
    "MAX_TOTAL_COMPRESSION_ATTEMPTS",
    "INEFFECTIVE_THRESHOLD",
    "MIN_EFFECTIVENESS_PCT",
    "PROTECTED_TOOLS",
    "LAST_TURN_RATIO_THRESHOLD",
    "COMPLETED_MAX_ITEMS",
    "KEY_DECISIONS_MAX_ITEMS",
    "CRITICAL_CONTEXT_MAX_ITEMS",
    "FILE_OPS_LIST_MAX_CHARS",
    "LATEST_USER_REQUEST_MAX_CHARS",
    "AUTO_CONTINUE_PROMPT",
    "MAX_OVERFLOW_RETRIES",
    "MAX_COMPRESS_ATTEMPTS_PER_TURN",
    "COMPACTION_COOLDOWN_ROUNDS",
    "PRUNE_TTL_SECONDS",
    "TRUNCATE_BUDGET_RATIO",
    "MIN_TOOL_RESULT_TOKENS_TO_TRUNCATE",
    "TRUNCATABLE_RECENT_SKIP",
    "TTL_REGISTRY_MAX_ENTRIES",
]


class TestTriggerThresholds:
    def test_preemptive_truncate_ratio(self):
        assert PREEMPTIVE_TRUNCATE_RATIO == 0.70
        assert isinstance(PREEMPTIVE_TRUNCATE_RATIO, float)

    def test_compression_trigger_ratio(self):
        assert COMPRESSION_TRIGGER_RATIO == 0.80
        assert isinstance(COMPRESSION_TRIGGER_RATIO, float)


class TestBudgetBasedTail:
    def test_min_preserve_tokens(self):
        assert MIN_PRESERVE_TOKENS == 2_000
        assert isinstance(MIN_PRESERVE_TOKENS, int)

    def test_max_preserve_tokens(self):
        assert MAX_PRESERVE_TOKENS == 15_000
        assert isinstance(MAX_PRESERVE_TOKENS, int)

    def test_preserve_ratio(self):
        assert PRESERVE_RATIO == 0.25
        assert isinstance(PRESERVE_RATIO, float)


class TestMultiStrategyPipeline:
    def test_prune_protect_tokens(self):
        assert PRUNE_PROTECT_TOKENS == 40_000
        assert isinstance(PRUNE_PROTECT_TOKENS, int)

    def test_prune_min_reduction_tokens(self):
        assert PRUNE_MIN_REDUCTION_TOKENS == 5_000
        assert isinstance(PRUNE_MIN_REDUCTION_TOKENS, int)

    def test_target_truncate_ratio(self):
        assert TARGET_TRUNCATE_RATIO == 0.5
        assert isinstance(TARGET_TRUNCATE_RATIO, float)

    def test_min_output_chars_to_truncate(self):
        assert MIN_OUTPUT_CHARS_TO_TRUNCATE == 500
        assert isinstance(MIN_OUTPUT_CHARS_TO_TRUNCATE, int)

    def test_max_tool_output_chars(self):
        assert MAX_TOOL_OUTPUT_CHARS == 2_000
        assert isinstance(MAX_TOOL_OUTPUT_CHARS, int)

    def test_aggressive_truncate_chars(self):
        assert AGGRESSIVE_TRUNCATE_CHARS == 1_000
        assert isinstance(AGGRESSIVE_TRUNCATE_CHARS, int)


class TestLLMSummaryImprovement:
    def test_summary_trim_tokens(self):
        assert SUMMARY_TRIM_TOKENS == 12_000
        assert isinstance(SUMMARY_TRIM_TOKENS, int)

    def test_summary_total_max_chars(self):
        assert SUMMARY_TOTAL_MAX_CHARS == 16_000
        assert isinstance(SUMMARY_TOTAL_MAX_CHARS, int)

    def test_content_head_ratio(self):
        assert CONTENT_HEAD_RATIO == 0.3
        assert isinstance(CONTENT_HEAD_RATIO, float)

    def test_content_tail_ratio(self):
        assert CONTENT_TAIL_RATIO == 0.3
        assert isinstance(CONTENT_TAIL_RATIO, float)


class TestDegradationMonitoring:
    def test_degradation_no_text_threshold(self):
        assert DEGRADATION_NO_TEXT_THRESHOLD == 3
        assert isinstance(DEGRADATION_NO_TEXT_THRESHOLD, int)

    def test_max_recovery_attempts(self):
        assert MAX_RECOVERY_ATTEMPTS == 2
        assert isinstance(MAX_RECOVERY_ATTEMPTS, int)


class TestAntiThrashing:
    def test_max_total_compression_attempts(self):
        assert MAX_TOTAL_COMPRESSION_ATTEMPTS == 5
        assert isinstance(MAX_TOTAL_COMPRESSION_ATTEMPTS, int)

    def test_ineffective_threshold(self):
        assert INEFFECTIVE_THRESHOLD == 2
        assert isinstance(INEFFECTIVE_THRESHOLD, int)

    def test_min_effectiveness_pct(self):
        assert MIN_EFFECTIVENESS_PCT == 0.05
        assert isinstance(MIN_EFFECTIVENESS_PCT, float)


class TestProtectedTools:
    def test_protected_tools_is_collection(self):
        assert isinstance(PROTECTED_TOOLS, (set, frozenset, tuple))

    def test_protected_tools_members(self):
        assert set(PROTECTED_TOOLS) == {"memory", "skill_view", "skill_list"}


class TestLastTurnDetection:
    def test_last_turn_ratio_threshold(self):
        assert LAST_TURN_RATIO_THRESHOLD == 0.5
        assert isinstance(LAST_TURN_RATIO_THRESHOLD, float)


class TestFIFOSectionLimits:
    def test_completed_max_items(self):
        assert COMPLETED_MAX_ITEMS == 5
        assert isinstance(COMPLETED_MAX_ITEMS, int)

    def test_key_decisions_max_items(self):
        assert KEY_DECISIONS_MAX_ITEMS == 5
        assert isinstance(KEY_DECISIONS_MAX_ITEMS, int)

    def test_critical_context_max_items(self):
        assert CRITICAL_CONTEXT_MAX_ITEMS == 3
        assert isinstance(CRITICAL_CONTEXT_MAX_ITEMS, int)


class TestFileOperationsRatchet:
    def test_file_ops_list_max_chars(self):
        assert FILE_OPS_LIST_MAX_CHARS == 900
        assert isinstance(FILE_OPS_LIST_MAX_CHARS, int)


class TestLatestUserRequest:
    def test_latest_user_request_max_chars(self):
        assert LATEST_USER_REQUEST_MAX_CHARS == 800
        assert isinstance(LATEST_USER_REQUEST_MAX_CHARS, int)


class TestAutoContinue:
    def test_auto_continue_prompt_is_str(self):
        assert isinstance(AUTO_CONTINUE_PROMPT, str)

    def test_auto_continue_prompt_value(self):
        assert AUTO_CONTINUE_PROMPT == (
            "Continue if you have next steps, or stop and ask for clarification "
            "if you are unsure how to proceed."
        )


class TestContextCompressionMultiTriggerDualTrack:
    def test_max_overflow_retries(self):
        assert MAX_OVERFLOW_RETRIES == 3
        assert isinstance(MAX_OVERFLOW_RETRIES, int)

    def test_max_compress_attempts_per_turn(self):
        assert MAX_COMPRESS_ATTEMPTS_PER_TURN == 3
        assert isinstance(MAX_COMPRESS_ATTEMPTS_PER_TURN, int)

    def test_compaction_cooldown_rounds(self):
        assert COMPACTION_COOLDOWN_ROUNDS == 3
        assert isinstance(COMPACTION_COOLDOWN_ROUNDS, int)

    def test_prune_ttl_seconds(self):
        assert PRUNE_TTL_SECONDS == 300
        assert isinstance(PRUNE_TTL_SECONDS, int)

    def test_truncate_budget_ratio(self):
        assert TRUNCATE_BUDGET_RATIO == 0.6
        assert isinstance(TRUNCATE_BUDGET_RATIO, float)

    def test_min_tool_result_tokens_to_truncate(self):
        assert MIN_TOOL_RESULT_TOKENS_TO_TRUNCATE == 200
        assert isinstance(MIN_TOOL_RESULT_TOKENS_TO_TRUNCATE, int)

    def test_truncatable_recent_skip(self):
        assert TRUNCATABLE_RECENT_SKIP == 6
        assert isinstance(TRUNCATABLE_RECENT_SKIP, int)

    def test_ttl_registry_max_entries(self):
        assert TTL_REGISTRY_MAX_ENTRIES == 512
        assert isinstance(TTL_REGISTRY_MAX_ENTRIES, int)


class TestContract:
    def test_all_36_contract_names_importable(self):
        num = importlib.import_module("config.num")
        missing = [name for name in CONTRACT_NAMES if not hasattr(num, name)]
        assert missing == [], f"missing contract constants: {missing}"

    def test_contract_name_count(self):
        assert len(CONTRACT_NAMES) == 36


class TestPreservedConstants:
    def test_legacy_compression_thresholds(self):
        num = importlib.import_module("config.num")
        assert num.ARCHIVE_THRESHOLD == 8_000
        assert num.MEMORY_THRESHOLD == 10_000
        assert num.COMPRESS_RATIO == 0.5

    def test_bus_queue_maxsize(self):
        num = importlib.import_module("config.num")
        assert num.BUS_QUEUE_MAXSIZE == 1000
        assert isinstance(num.BUS_QUEUE_MAXSIZE, int)

    def test_chars_per_token(self):
        num = importlib.import_module("config.num")
        assert num.CHARS_PER_TOKEN == 4
        assert isinstance(num.CHARS_PER_TOKEN, int)
