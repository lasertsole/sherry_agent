"""Single source of truth for session state-register keys.

Values are frozen contracts: they are persisted verbatim in
``state_register.db`` (``states.key``) and read back by string, so every member
value MUST equal the literal used before this module existed. ``StateKey`` is a
``StrEnum`` (a ``str`` subclass whose hash and equality match plain strings), so
string comparisons, SQL bindings, JSON round-trips and dict lookups against
pre-existing literals keep working unchanged.
"""

from enum import StrEnum
from typing import Any


class StateKey(StrEnum):
    SYSTEM_PROMPT = "system_prompt"

    ITERATION_BUDGET = "iteration_budget"
    ITERATION_BUDGET_USED = "iteration_budget_used"
    TOOL_GUARDRAIL_STATE = "tool_guardrail_state"
    IS_STREAM_TURN = "is_stream_turn"

    LLM_STALE_STREAK = "llm_stale_streak"
    LLM_FALLBACK_INDEX = "llm_fallback_index"
    LLM_CONTENT_FILTER_BLOCKED = "llm_content_filter_blocked"
    LLM_CONTENT_FILTER_TERMINATED = "llm_content_filter_terminated"
    LLM_PARTIAL_STREAM_STUB = "llm_partial_stream_stub"
    LLM_PARTIAL_STREAM_CAUSE = "llm_partial_stream_cause"

    # Per-session thinking/reasoning toggle (client UI → /sessions/thinking).
    # bool when the user made an explicit choice; absent = env default.
    LLM_THINKING_ENABLED = "llm_thinking_enabled"

    MULTIMODAL_TRYING_NATIVE = "_multimodal_trying_native"
    MULTIMODAL_NATIVE_MODEL = "_multimodal_native_model"

    HEARTBEAT_ITER = "heartbeat_iter"
    HEARTBEAT_TOOL = "heartbeat_tool"
    HEARTBEAT_STALE = "heartbeat_stale"
    HEARTBEAT_KILLED = "heartbeat_killed"
    HEARTBEAT_SKIP = "heartbeat_skip"
    HEARTBEAT_LAST_ITER = "_last_heartbeat_iter"
    HEARTBEAT_LAST_TOOL = "_last_heartbeat_tool"

    OUTPUT_REPETITION_HISTORY = "output_repetition_history"
    OUTPUT_REPETITION_WARN_COUNT = "output_repetition_warn_count"
    OUTPUT_REPETITION_INTERNAL_WARNED = "output_repetition_internal_warned"
    OUTPUT_REPETITION_HALTED = "output_repetition_halted"
    OUTPUT_REPETITION_REASONING_HISTORY = "output_repetition_reasoning_history"
    OUTPUT_REPETITION_REASONING_WARNED = "output_repetition_reasoning_warned"

    SUMMARIZATION_LAST_USER_QUESTION = "summarization_last_user_question"
    SUMMARIZATION_DEGRADATION_NO_TEXT = "summarization_degradation_no_text"
    SUMMARIZATION_RECOVERY_ATTEMPTS = "summarization_recovery_attempts"
    SUMMARIZATION_PREVIOUS_FILE_OPS = "summarization_previous_file_ops"
    SUMMARIZATION_COOLDOWN_ROUNDS = "summarization_cooldown_rounds"
    SUMMARIZATION_TURN_ATTEMPTS = "summarization_turn_attempts"
    SUMMARIZATION_OVERFLOW_RETRIES = "summarization_overflow_retries"
    SUMMARIZATION_COMPRESSION_COUNT = "summarization_compression_count"
    SUMMARIZATION_COMPRESSION_INEFFECTIVE = "summarization_compression_ineffective"
    SUMMARIZATION_COMPRESSION_LAST_TOKENS = "summarization_compression_last_tokens"
    SUMMARIZATION_LAST_STRATEGY = "summarization_last_strategy"
    SUMMARIZATION_SKIP_LLM = "summarization_skip_llm"
    SUMMARIZATION_FORCE_RECOVERY = "summarization_force_recovery"

    NUDGE_PLAN_EXTRACTION_LOCK = "nudge_plan_extraction_lock"
    NUDGE_PLAN_EXTRACTION_FIRED = "nudge_plan_extraction_fired"
    NUDGE_REVIEW_MEMORY_LOCK = "nudge_review_memory_lock"
    COMPRESSION_TODO_UPDATE_LOCK = "compression_todo_update_lock"
    PLAN_REF = "plan_ref"

    HITL_PERMANENT = "hitl:permanent"
    HITL_SESSION_APPROVED = "hitl:session_approved"
    HITL_SESSION_YOLO = "hitl:session_yolo"
    HITL_TURN_INTERRUPTED = "hitl:turn_interrupted"

    EXTERNAL_PATH_YOLO = "external_path_yolo"
    EXTERNAL_PATH_ALLOWLIST = "external_path_allowlist"

    ANSWERING = "answering"
    REQUESTER_SESSION_KEY = "requester_session_key"
    CALLER_SCOPE = "caller_scope"


def hitl_tool_approved_key(tool_name: str) -> str:
    """Return the HITL per-tool approval key for *tool_name*."""
    return f"hitl:tool_approved:{tool_name}"


class TypedState:
    """Typed facade over the in-memory session state register.

    Middlewares pass a :class:`StateKey` (or a builder-produced key string)
    instead of a bare literal. The register's public API, its stored key values
    and its lifetime are unchanged; ``state_register_mem`` is resolved lazily so
    importing this module stays side-effect-free.
    """

    @staticmethod
    def set(session_id: str, key: StateKey | str, value: Any) -> bool:
        from runtime.session.state_register import state_register_mem

        return state_register_mem.set_state(session_id, key, value)

    @staticmethod
    def get(session_id: str, key: StateKey | str, default: Any = None) -> Any:
        from runtime.session.state_register import state_register_mem

        return state_register_mem.get_state(session_id, key, default)

    @staticmethod
    def delete(session_id: str, key: StateKey | str) -> bool:
        from runtime.session.state_register import state_register_mem

        return state_register_mem.delete_state(session_id, key)

    @staticmethod
    def has_key(session_id: str, key: StateKey | str) -> bool:
        from runtime.session.state_register import state_register_mem

        return state_register_mem.has_key(session_id, key)

    @staticmethod
    def update(session_id: str, states: dict[str, Any]) -> bool:
        from runtime.session.state_register import state_register_mem

        return state_register_mem.update_states(session_id, states)

    @staticmethod
    def clear(session_id: str) -> bool:
        from runtime.session.state_register import state_register_mem

        return state_register_mem.clear_session(session_id)
