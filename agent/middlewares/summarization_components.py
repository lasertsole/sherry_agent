"""Components extracted from the Summarization middleware.

* :class:`CompressionEffectivenessTracker` — anti-flutter bookkeeping over
  the session-scoped ``summarization_*`` state keys (was
  ``Summarization._record_compression`` / ``._should_skip_compression``).
* :class:`MessageTruncator` — head/tail content cutting, preemptive and
  aggressive truncation of oversized tool payloads.
* :class:`OrphanPairRepairer` — walks the cutoff backwards until no
  ``ToolMessage`` is separated from its ``AIMessage`` tool-call.

The middleware keeps hook coordination and delegates to these.
"""

from __future__ import annotations

import json

from loguru import logger

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, ToolMessage

from config.num import (
    AGGRESSIVE_TRUNCATE_CHARS,
    CONTENT_HEAD_RATIO,
    CONTENT_TAIL_RATIO,
    INEFFECTIVE_THRESHOLD,
    MAX_TOTAL_COMPRESSION_ATTEMPTS,
    MIN_EFFECTIVENESS_PCT,
    PROTECTED_TOOLS,
    SUMMARY_TOTAL_MAX_CHARS,
)

from runtime import state_register_mem

# Anti-flutter / anti-thrash state keys (re-exported by the middleware module,
# whose tests read them via getattr on the module).
_COMPRESSION_COUNT_KEY = "summarization_compression_count"
_COMPRESSION_INEFFECTIVE_KEY = "summarization_compression_ineffective"
_COMPRESSION_LAST_TOKENS_KEY = "summarization_compression_last_tokens"
_LAST_STRATEGY_KEY = "summarization_last_strategy"
_SKIP_LLM_KEY = "summarization_skip_llm"
_FORCE_RECOVERY_KEY = "summarization_force_recovery"

_PREEMPTIVE_TRUNCATE_MAX_CHARS = 2000

_SUMMARY_LC_SOURCE = "summarization"


class CompressionEffectivenessTracker:
    """Anti-flutter: track whether the last compression actually saved tokens.

    ``record`` updates the per-session compression counters; ``should_skip``
    answers whether compression must be skipped (attempt cap reached, or the
    ineffective streak disabled the LLM step). The ``force recovery`` flag is
    consumed here exactly as before.
    """

    def __init__(self, estimate_tokens):
        self._estimate_tokens = estimate_tokens

    def should_skip(self, session_id: str) -> bool:
        if state_register_mem.get_state(session_id, _FORCE_RECOVERY_KEY, False):
            state_register_mem.set_state(session_id, _FORCE_RECOVERY_KEY, False)
            state_register_mem.set_state(session_id, _SKIP_LLM_KEY, False)
            state_register_mem.set_state(session_id, _COMPRESSION_COUNT_KEY, 0)
            state_register_mem.set_state(session_id, _COMPRESSION_INEFFECTIVE_KEY, 0)
            return False

        attempts = state_register_mem.get_state(session_id, _COMPRESSION_COUNT_KEY, 0)
        if attempts >= MAX_TOTAL_COMPRESSION_ATTEMPTS:
            logger.debug("Max compression attempts ({}) reached", MAX_TOTAL_COMPRESSION_ATTEMPTS)
            return True

        ineffective = state_register_mem.get_state(session_id, _COMPRESSION_INEFFECTIVE_KEY, 0)
        if ineffective >= INEFFECTIVE_THRESHOLD:
            if not state_register_mem.get_state(session_id, _SKIP_LLM_KEY, False):
                state_register_mem.set_state(session_id, _SKIP_LLM_KEY, True)
                logger.debug("LLM summary ineffective, switching to non-LLM strategies only")
            return False

        return False

    def record(
        self,
        session_id: str,
        before_messages,
        after_messages,
        strategy_used: str = "",
    ) -> None:
        attempts = state_register_mem.get_state(session_id, _COMPRESSION_COUNT_KEY, 0) + 1
        state_register_mem.set_state(session_id, _COMPRESSION_COUNT_KEY, attempts)
        state_register_mem.set_state(session_id, _LAST_STRATEGY_KEY, strategy_used or "unknown")

        before_tokens = self._estimate_tokens(before_messages)
        after_tokens = self._estimate_tokens(after_messages)
        msg_reduced = len(after_messages) < len(before_messages)
        token_reduction_pct = (
            (before_tokens - after_tokens) / before_tokens if before_tokens > 0 else 0.0
        )
        effective = msg_reduced or token_reduction_pct >= MIN_EFFECTIVENESS_PCT

        if not effective:
            ineffective = (
                state_register_mem.get_state(session_id, _COMPRESSION_INEFFECTIVE_KEY, 0) + 1
            )
            state_register_mem.set_state(session_id, _COMPRESSION_INEFFECTIVE_KEY, ineffective)
        else:
            state_register_mem.set_state(session_id, _COMPRESSION_INEFFECTIVE_KEY, 0)
            if strategy_used in ("dedup", "prune", "truncate", "fallback", "aggressive"):
                state_register_mem.set_state(session_id, _SKIP_LLM_KEY, False)

        state_register_mem.set_state(session_id, _COMPRESSION_LAST_TOKENS_KEY, after_tokens)


class MessageTruncator:
    """Cut messages at a size ceiling: summary re-truncation, preemptive
    tool-output truncation and the aggressive last-resort pass."""

    @staticmethod
    def truncate_content(content: str, max_chars: int) -> str:
        if len(content) <= max_chars:
            return content
        head = content[: int(max_chars * CONTENT_HEAD_RATIO)]
        tail = content[-int(max_chars * CONTENT_TAIL_RATIO) :]
        omitted = len(content) - len(head) - len(tail)
        return f"{head}...[omitted {omitted} chars]...{tail}"

    @staticmethod
    def truncate_summary_messages(messages: list[BaseMessage]) -> list[BaseMessage]:
        result: list[BaseMessage] = []
        for m in messages:
            if getattr(m, "additional_kwargs", {}).get("lc_source") == _SUMMARY_LC_SOURCE:
                content = getattr(m, "content", "")
                if isinstance(content, str) and len(content) > SUMMARY_TOTAL_MAX_CHARS:
                    truncated = MessageTruncator.truncate_content(content, SUMMARY_TOTAL_MAX_CHARS)
                    m = m.model_copy(update={"content": truncated})
            result.append(m)
        return result

    def preemptive_truncate(
        self, messages: list[BaseMessage], session_id: str
    ) -> list[BaseMessage]:
        result: list[BaseMessage] = []
        truncated_count = 0

        for m in messages:
            if isinstance(m, ToolMessage):
                tc_id = getattr(m, "tool_call_id", "")
                tool_name = find_tool_name(messages, m, tc_id)
                if tool_name in PROTECTED_TOOLS:
                    result.append(m)
                    continue
                content = str(getattr(m, "content", ""))
                if len(content) > _PREEMPTIVE_TRUNCATE_MAX_CHARS:
                    head = content[: int(_PREEMPTIVE_TRUNCATE_MAX_CHARS * CONTENT_HEAD_RATIO)]
                    tail = content[-int(_PREEMPTIVE_TRUNCATE_MAX_CHARS * CONTENT_TAIL_RATIO) :]
                    omitted = len(content) - len(head) - len(tail)
                    truncated = f"{head}...[omitted {omitted} chars]...{tail}"
                    result.append(m.model_copy(update={"content": truncated}))
                    truncated_count += 1
                else:
                    result.append(m)
            else:
                result.append(m)

        if truncated_count > 0:
            logger.debug(
                "Preemptive truncation: {} tool outputs, session={}",
                truncated_count,
                session_id,
            )
        return result

    @staticmethod
    def aggressive_truncate(messages: list[BaseMessage]) -> list[BaseMessage]:
        result: list[BaseMessage] = []
        for msg in messages:
            if isinstance(msg, ToolMessage):
                content = str(getattr(msg, "content", ""))
                if len(content) > AGGRESSIVE_TRUNCATE_CHARS:
                    truncated = content[:AGGRESSIVE_TRUNCATE_CHARS] + (
                        f"...[aggressively truncated, {len(content) - AGGRESSIVE_TRUNCATE_CHARS} chars omitted]"
                    )
                    msg = msg.model_copy(update={"content": truncated})
            elif isinstance(msg, AIMessage) and getattr(msg, "tool_calls", None):
                new_tcs = []
                for tc in msg.tool_calls:
                    name = tc.get("name", "")
                    if name in PROTECTED_TOOLS:
                        new_tcs.append(tc)
                        continue
                    args = tc.get("args", {})
                    try:
                        args_str = json.dumps(args, ensure_ascii=False)
                    except (TypeError, ValueError):
                        args_str = str(args)
                    if len(args_str) > AGGRESSIVE_TRUNCATE_CHARS:
                        truncated = args_str[:AGGRESSIVE_TRUNCATE_CHARS] + (
                            f"...[args aggressively truncated, "
                            f"{len(args_str) - AGGRESSIVE_TRUNCATE_CHARS} chars omitted]"
                        )
                        new_tcs.append({**tc, "args": {"_truncated_args": truncated}})
                    else:
                        new_tcs.append(tc)
                msg = msg.model_copy(update={"tool_calls": new_tcs})
            result.append(msg)
        return result


class OrphanPairRepairer:
    """Fix orphan AI/Tool messages at the cutoff boundary."""

    @staticmethod
    def adjust(messages: list[BaseMessage], cutoff: int) -> int:
        adjusted = cutoff
        while adjusted > 0:
            orphan_ids: set[str] = set()
            for m in messages[adjusted:]:
                if isinstance(m, ToolMessage) and m.tool_call_id:
                    orphan_ids.add(m.tool_call_id)
            for m in messages[adjusted:]:
                if isinstance(m, AIMessage) and getattr(m, "tool_calls", None):
                    for tc in m.tool_calls:
                        orphan_ids.discard(tc.get("id"))
            if not orphan_ids:
                break

            earliest_orphan_ai = len(messages)
            for i in range(adjusted):
                m = messages[i]
                if isinstance(m, AIMessage) and getattr(m, "tool_calls", None):
                    if any(tc.get("id") in orphan_ids for tc in m.tool_calls):
                        earliest_orphan_ai = min(earliest_orphan_ai, i)
            if earliest_orphan_ai < adjusted:
                adjusted = earliest_orphan_ai
            else:
                prev_user_idx = next(
                    (
                        i
                        for i in range(adjusted - 1, -1, -1)
                        if isinstance(messages[i], HumanMessage)
                    ),
                    None,
                )
                if prev_user_idx is None:
                    break
                adjusted = prev_user_idx
        return adjusted


def find_tool_name(messages: list[BaseMessage], tool_msg: ToolMessage, tc_id: str) -> str:
    if not tc_id:
        return ""
    idx = messages.index(tool_msg)
    for i in range(idx - 1, -1, -1):
        m = messages[i]
        if isinstance(m, AIMessage) and getattr(m, "tool_calls", None):
            for tc in m.tool_calls:
                if tc.get("id") == tc_id:
                    return tc.get("name", "")
    return ""
