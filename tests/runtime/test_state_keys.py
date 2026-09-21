"""Unit tests for ``runtime.session.state_keys`` — the typed key single source.

The persisted ``states.key`` column stores the literal string, so a drifted
:class:`StateKey` value would make previously written state unreadable. These
tests freeze every member's value and pin the ``str`` compatibility contract.
"""

import pytest

from runtime.session.state_keys import StateKey, TypedState, hitl_tool_approved_key
from runtime.session.state_register import StateRegisterMeM

pytestmark = [pytest.mark.unit]


# Frozen snapshot of the pre-StrEnum literals. Every value MUST match exactly.
EXPECTED_VALUES: dict[str, str] = {
    "SYSTEM_PROMPT": "system_prompt",
    "ITERATION_BUDGET": "iteration_budget",
    "ITERATION_BUDGET_USED": "iteration_budget_used",
    "TOOL_GUARDRAIL_STATE": "tool_guardrail_state",
    "IS_STREAM_TURN": "is_stream_turn",
    "LLM_STALE_STREAK": "llm_stale_streak",
    "LLM_FALLBACK_INDEX": "llm_fallback_index",
    "LLM_CONTENT_FILTER_BLOCKED": "llm_content_filter_blocked",
    "LLM_CONTENT_FILTER_TERMINATED": "llm_content_filter_terminated",
    "LLM_PARTIAL_STREAM_STUB": "llm_partial_stream_stub",
    "LLM_PARTIAL_STREAM_CAUSE": "llm_partial_stream_cause",
    "MULTIMODAL_TRYING_NATIVE": "_multimodal_trying_native",
    "MULTIMODAL_NATIVE_MODEL": "_multimodal_native_model",
    "HEARTBEAT_ITER": "heartbeat_iter",
    "HEARTBEAT_TOOL": "heartbeat_tool",
    "HEARTBEAT_STALE": "heartbeat_stale",
    "HEARTBEAT_KILLED": "heartbeat_killed",
    "HEARTBEAT_SKIP": "heartbeat_skip",
    "HEARTBEAT_LAST_ITER": "_last_heartbeat_iter",
    "HEARTBEAT_LAST_TOOL": "_last_heartbeat_tool",
    "OUTPUT_REPETITION_HISTORY": "output_repetition_history",
    "OUTPUT_REPETITION_WARN_COUNT": "output_repetition_warn_count",
    "OUTPUT_REPETITION_INTERNAL_WARNED": "output_repetition_internal_warned",
    "OUTPUT_REPETITION_HALTED": "output_repetition_halted",
    "OUTPUT_REPETITION_REASONING_HISTORY": "output_repetition_reasoning_history",
    "OUTPUT_REPETITION_REASONING_WARNED": "output_repetition_reasoning_warned",
    "SUMMARIZATION_LAST_USER_QUESTION": "summarization_last_user_question",
    "SUMMARIZATION_DEGRADATION_NO_TEXT": "summarization_degradation_no_text",
    "SUMMARIZATION_RECOVERY_ATTEMPTS": "summarization_recovery_attempts",
    "SUMMARIZATION_PREVIOUS_FILE_OPS": "summarization_previous_file_ops",
    "SUMMARIZATION_COOLDOWN_ROUNDS": "summarization_cooldown_rounds",
    "SUMMARIZATION_TURN_ATTEMPTS": "summarization_turn_attempts",
    "SUMMARIZATION_OVERFLOW_RETRIES": "summarization_overflow_retries",
    "SUMMARIZATION_COMPRESSION_COUNT": "summarization_compression_count",
    "SUMMARIZATION_COMPRESSION_INEFFECTIVE": "summarization_compression_ineffective",
    "SUMMARIZATION_COMPRESSION_LAST_TOKENS": "summarization_compression_last_tokens",
    "SUMMARIZATION_LAST_STRATEGY": "summarization_last_strategy",
    "SUMMARIZATION_SKIP_LLM": "summarization_skip_llm",
    "SUMMARIZATION_FORCE_RECOVERY": "summarization_force_recovery",
    "NUDGE_PLAN_EXTRACTION_LOCK": "nudge_plan_extraction_lock",
    "NUDGE_PLAN_EXTRACTION_FIRED": "nudge_plan_extraction_fired",
    "NUDGE_REVIEW_MEMORY_LOCK": "nudge_review_memory_lock",
    "COMPRESSION_TODO_UPDATE_LOCK": "compression_todo_update_lock",
    "PLAN_REF": "plan_ref",
    "HITL_PERMANENT": "hitl:permanent",
    "HITL_SESSION_APPROVED": "hitl:session_approved",
    "HITL_SESSION_YOLO": "hitl:session_yolo",
    "HITL_TURN_INTERRUPTED": "hitl:turn_interrupted",
    "EXTERNAL_PATH_YOLO": "external_path_yolo",
    "EXTERNAL_PATH_ALLOWLIST": "external_path_allowlist",
    "ANSWERING": "answering",
    "REQUESTER_SESSION_KEY": "requester_session_key",
    "CALLER_SCOPE": "caller_scope",
}


class TestStateKeyValues:
    def test_no_member_missing_or_extra(self):
        assert {member.name for member in StateKey} == set(EXPECTED_VALUES)

    @pytest.mark.parametrize(("name", "value"), sorted(EXPECTED_VALUES.items()))
    def test_member_value_is_frozen_literal(self, name, value):
        assert StateKey[name].value == value

    def test_str_equality_and_hash_match_plain_literal(self):
        # Persisted state is read back as a plain str; lookups must not miss.
        assert StateKey.SYSTEM_PROMPT == "system_prompt"
        assert hash(StateKey.SYSTEM_PROMPT) == hash("system_prompt")
        container = {"system_prompt": 7}
        assert container.get(StateKey.SYSTEM_PROMPT) == 7
        assert {StateKey.SYSTEM_PROMPT: 8}.get("system_prompt") == 8

    def test_hitl_tool_approved_key(self):
        assert hitl_tool_approved_key("bash") == "hitl:tool_approved:bash"


class TestTypedStateFacade:
    @pytest.fixture
    def mem(self):
        from runtime.session.core import SessionRegister

        if StateRegisterMeM in SessionRegister._instances:
            del SessionRegister._instances[StateRegisterMeM]
        return StateRegisterMeM()

    def test_facade_roundtrip(self, mem, monkeypatch):
        import runtime.session.state_register as state_register_mod

        monkeypatch.setattr(state_register_mod, "state_register_mem", mem)
        sid = "facade-session"

        assert TypedState.set(sid, StateKey.SYSTEM_PROMPT, "PROMPT") is True
        assert TypedState.get(sid, StateKey.SYSTEM_PROMPT) == "PROMPT"
        assert TypedState.has_key(sid, StateKey.SYSTEM_PROMPT) is True
        assert TypedState.update(sid, {StateKey.ANSWERING: True}) is True
        assert TypedState.get(sid, StateKey.ANSWERING) is True
        assert TypedState.delete(sid, StateKey.ANSWERING) is True
        assert TypedState.has_key(sid, StateKey.ANSWERING) is False
        mem.clear_session(sid)


class TestMigratedConstantsResolveToFrozenValues:
    def test_repetition_session_keys(self):
        from agent.middlewares.output_repetition_guard.repetition_state import (
            SESSION_STATE_KEYS,
        )

        assert tuple(str(key) for key in SESSION_STATE_KEYS) == (
            "output_repetition_history",
            "output_repetition_warn_count",
            "output_repetition_internal_warned",
            "output_repetition_halted",
            "output_repetition_reasoning_history",
            "output_repetition_reasoning_warned",
        )

    def test_hitl_types_constants(self):
        from agent.middlewares.humanInTheLoop.types import (
            HITL_PERMANENT_KEY,
            HITL_SESSION_APPROVED_KEY,
            HITL_TURN_INTERRUPTED_KEY,
            SESSION_YOLO_KEY,
        )

        assert SESSION_YOLO_KEY == "hitl:session_yolo"
        assert HITL_PERMANENT_KEY == "hitl:permanent"
        assert HITL_SESSION_APPROVED_KEY == "hitl:session_approved"
        assert HITL_TURN_INTERRUPTED_KEY == "hitl:turn_interrupted"

    def test_iteration_budget_constants(self):
        from agent.middlewares.iteration_budget.core import IterationBudget

        assert IterationBudget._BUDGET_KEY == "iteration_budget"
        assert IterationBudget._USED_KEY == "iteration_budget_used"
