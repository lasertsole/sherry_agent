"""Anti-thrash counters and cooldown bookkeeping for summarization.

The durable cooldown mirror (``_persist_cooldown_state`` / ``_restore_*``) stays in
``summarization.core`` because tests patch its ``state_register_db`` seam on the
core module.
"""

from collections.abc import Sequence

from loguru import logger
from langchain_core.messages import AnyMessage, BaseMessage, HumanMessage

from config.features import SUMMARIZATION
from runtime import state_register_mem

from .state_aliases import (
    _COMPRESSION_COUNT_KEY,
    _COMPRESSION_INEFFECTIVE_KEY,
    _COMPRESSION_LAST_TOKENS_KEY,
    _COOLDOWN_ROUNDS_KEY,
    _DEGRADATION_NO_TEXT_KEY,
    _FORCE_RECOVERY_KEY,
    _LAST_STRATEGY_KEY,
    _LAST_USER_QUESTION_KEY,
    _OVERFLOW_RETRIES_KEY,
    _PREVIOUS_FILE_OPS_KEY,
    _RECOVERY_ATTEMPTS_KEY,
    _SKIP_LLM_KEY,
    _TURN_ATTEMPTS_KEY,
)
from .summarization_components import (
    INEFFECTIVE_THRESHOLD,
    MAX_TOTAL_COMPRESSION_ATTEMPTS,
)

LAST_TURN_RATIO_THRESHOLD = SUMMARIZATION["last_turn_ratio_threshold"]
DEGRADATION_NO_TEXT_THRESHOLD = SUMMARIZATION["degradation_no_text_threshold"]
MAX_RECOVERY_ATTEMPTS = SUMMARIZATION["max_recovery_attempts"]


class ThrashMixin:
    """Compression counters, cooldown ticks, degradation monitoring."""

    @staticmethod
    def _slice_last_turn(messages: list[AnyMessage]) -> list[AnyMessage]:
        if not messages:
            return []
        last_user_idx = next(
            (i for i in range(len(messages) - 1, -1, -1) if isinstance(messages[i], HumanMessage)),
            None,
        )
        if last_user_idx is None:
            return []
        return messages[last_user_idx:]

    def _check_last_turn_ratio(self, messages: list[AnyMessage], session_id: str) -> bool:
        total_tokens = self._estimate_tokens(messages)
        if total_tokens <= 0:
            self._compress_last_turn = False
            return False
        last_turn = self._slice_last_turn(messages)
        last_turn_tokens = self._estimate_tokens(last_turn)
        ratio = last_turn_tokens / total_tokens
        compress = ratio >= LAST_TURN_RATIO_THRESHOLD
        self._compress_last_turn = compress
        if compress:
            last_user_msg = next(
                (m for m in reversed(messages) if isinstance(m, HumanMessage)), None
            )
            question = (
                last_user_msg.content
                if last_user_msg and isinstance(last_user_msg.content, str)
                else ""
            )
            state_register_mem.set_state(session_id, _LAST_USER_QUESTION_KEY, question)
        else:
            state_register_mem.set_state(session_id, _LAST_USER_QUESTION_KEY, "")
        logger.debug(
            "Compaction: last-turn ratio={:.1f}%, compress_last_turn={}, session={}",
            ratio * 100,
            compress,
            session_id,
        )
        return compress

    def _tick_cooldown(self, session_id: str) -> bool:
        """Cooldown bookkeeping: EVERY wrap_model_call decrements when >0.

        Returns True when the decrement happened (T2 proactive trigger is
        suppressed for that call). Cooldown blocks proactive compression
        only — forced recovery is exempt (caller checks the force flag).
        """
        rounds = state_register_mem.get_state(session_id, _COOLDOWN_ROUNDS_KEY, 0) or 0
        if rounds > 0:
            state_register_mem.set_state(session_id, _COOLDOWN_ROUNDS_KEY, rounds - 1)
            return True
        return False

    def _cooldown_degraded(self, session_id: str) -> bool:
        skip_llm = state_register_mem.get_state(session_id, _SKIP_LLM_KEY, False)
        ineffective = state_register_mem.get_state(session_id, _COMPRESSION_INEFFECTIVE_KEY, 0) or 0
        attempts = state_register_mem.get_state(session_id, _COMPRESSION_COUNT_KEY, 0) or 0
        return (
            bool(skip_llm)
            or ineffective >= INEFFECTIVE_THRESHOLD
            or attempts >= MAX_TOTAL_COMPRESSION_ATTEMPTS
        )

    def _should_skip_compression(self, session_id: str) -> bool:
        was_degraded = self._cooldown_degraded(session_id)
        result = self._effectiveness_tracker.should_skip(session_id)
        if was_degraded or self._cooldown_degraded(session_id):
            self._persist_cooldown_state(session_id)
        return result

    def _record_compression(
        self,
        session_id: str,
        before_messages: Sequence[BaseMessage],
        after_messages: Sequence[BaseMessage],
        strategy_used: str = "",
    ) -> None:
        was_degraded = self._cooldown_degraded(session_id)
        self._effectiveness_tracker.record(
            session_id, before_messages, after_messages, strategy_used
        )
        if was_degraded or self._cooldown_degraded(session_id):
            self._persist_cooldown_state(session_id)

    def _monitor_degradation(self, response, session_id: str):
        if not self._compaction_just_happened:
            return
        self._compaction_just_happened = False

        if self._is_empty_response(response):
            count = state_register_mem.get_state(session_id, _DEGRADATION_NO_TEXT_KEY, 0) + 1
            state_register_mem.set_state(session_id, _DEGRADATION_NO_TEXT_KEY, count)

            if count >= DEGRADATION_NO_TEXT_THRESHOLD:
                attempts = state_register_mem.get_state(session_id, _RECOVERY_ATTEMPTS_KEY, 0)
                if attempts < MAX_RECOVERY_ATTEMPTS:
                    was_degraded = self._cooldown_degraded(session_id)
                    state_register_mem.set_state(session_id, _RECOVERY_ATTEMPTS_KEY, attempts + 1)
                    state_register_mem.set_state(session_id, _FORCE_RECOVERY_KEY, True)
                    state_register_mem.set_state(session_id, _COMPRESSION_INEFFECTIVE_KEY, 0)
                    state_register_mem.set_state(session_id, _COMPRESSION_COUNT_KEY, 0)
                    if was_degraded:
                        self._persist_cooldown_state(session_id)
                    logger.warning(
                        "Degradation detected ({} empty responses), forcing recovery",
                        count,
                    )
        else:
            state_register_mem.set_state(session_id, _DEGRADATION_NO_TEXT_KEY, 0)

    def _reset_turn_state(self, session_id: str) -> None:
        state_register_mem.set_state(session_id, _COMPRESSION_COUNT_KEY, 0)
        state_register_mem.set_state(session_id, _COMPRESSION_INEFFECTIVE_KEY, 0)
        state_register_mem.set_state(session_id, _COMPRESSION_LAST_TOKENS_KEY, None)
        state_register_mem.set_state(session_id, _SKIP_LLM_KEY, False)
        state_register_mem.set_state(session_id, _LAST_STRATEGY_KEY, "")
        state_register_mem.set_state(session_id, _DEGRADATION_NO_TEXT_KEY, 0)
        state_register_mem.set_state(session_id, _RECOVERY_ATTEMPTS_KEY, 0)
        state_register_mem.set_state(session_id, _FORCE_RECOVERY_KEY, False)
        state_register_mem.set_state(session_id, _PREVIOUS_FILE_OPS_KEY, None)
        # NEW: per-turn proactive-compression attempt counter.
        state_register_mem.set_state(session_id, _TURN_ATTEMPTS_KEY, 0)
        state_register_mem.set_state(session_id, _OVERFLOW_RETRIES_KEY, 0)
