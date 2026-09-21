"""Compression application: cutoff, non-LLM strategies, under-lock pipeline.

The two under-lock variants stay duplicated on purpose: the sync path runs the
pre-compression memory flush via ``run_memory_flush_sync`` while the async path
awaits ``run_memory_flush``, so collapsing them would change cancel/await
semantics.
"""

# allow: SIZE_OK — the bulk is the two under-lock application variants that
# must stay duplicated (sync ``run_memory_flush_sync`` vs awaited
# ``run_memory_flush``) plus the multi-strategy pipeline that belongs beside
# them; splitting the pipeline from the application would scatter the
# compression contract.

import re
from typing import Any, cast

from langchain.agents.middleware import ModelRequest
from langchain_core.messages import (
    AIMessage,
    AnyMessage,
    BaseMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)
from langgraph.typing import ContextT
from loguru import logger

from config.features import SUMMARIZATION
from pub.func.message.target_truncation import target_truncate_tool_outputs
from pub.func.message.tool_args_truncate import truncate_tool_args
from pub.func.message.tool_output_dedup import dedup_tool_outputs
from pub.func.message.tool_output_prune import prune_tool_outputs
from pub.func.message.turn_utils import split_into_turns, split_turn
from runtime import state_register_mem

from .compaction_lock import CompactionLock, CompactionLockError
from .media_offload import offload_inline_media
from .state_aliases import _LAST_USER_QUESTION_KEY, _PREVIOUS_FILE_OPS_KEY, _SKIP_LLM_KEY
from .summarization_components import MessageTruncator, find_tool_name
from .summary_generation import (
    _SUMMARY_CLOSE_TAG,
    _SUMMARY_LC_SOURCE,
    _build_static_fallback_summary,
    _extract_file_operations,
    _filter_summary_messages,
    _format_file_ops,
    _labeled_file_ops,
)

PRESERVE_RATIO = SUMMARIZATION["preserve_ratio"]
MIN_PRESERVE_TOKENS = SUMMARIZATION["min_preserve_tokens"]
MAX_PRESERVE_TOKENS = SUMMARIZATION["max_preserve_tokens"]
PRUNE_PROTECT_TOKENS = SUMMARIZATION["prune_protect_tokens"]
PRUNE_MIN_REDUCTION_TOKENS = SUMMARIZATION["prune_min_reduction_tokens"]
TARGET_TRUNCATE_RATIO = SUMMARIZATION["target_truncate_ratio"]
MIN_OUTPUT_CHARS_TO_TRUNCATE = SUMMARIZATION["min_output_chars_to_truncate"]
MAX_TOOL_OUTPUT_CHARS = SUMMARIZATION["max_tool_output_chars"]
MIN_ARGS_CHARS_TO_TRUNCATE = SUMMARIZATION["min_args_chars_to_truncate"]
MAX_TOOL_ARGS_CHARS = SUMMARIZATION["max_tool_args_chars"]
PROTECTED_TOOLS = SUMMARIZATION["protected_tools"]
LATEST_USER_REQUEST_MAX_CHARS = SUMMARIZATION["latest_user_request_max_chars"]


class CompressionMixin:
    """Budget-based cutoff, multi-strategy pipeline, under-lock application."""

    def _calculate_preserve_budget(self) -> int:
        ctx = self._main_llm_context_window
        if ctx:
            budget = int(ctx * PRESERVE_RATIO)
            return min(MAX_PRESERVE_TOKENS, max(MIN_PRESERVE_TOKENS, budget))
        return MIN_PRESERVE_TOKENS

    @property
    def _compaction_lock(self) -> CompactionLock:
        cached = self.__dict__.get("_compaction_lock_instance")
        if cached is None:
            cached = CompactionLock()
            self.__dict__["_compaction_lock_instance"] = cached
        return cached

    def _apply_compression(
        self,
        request: ModelRequest[ContextT],
        session_id: str,
    ) -> ModelRequest[ContextT]:
        """Serialize per-session compactions; fail-open."""
        try:
            with self._compaction_lock.acquire_sync(session_id):
                return self._apply_compression_under_lock(request, session_id)
        except CompactionLockError as exc:
            logger.warning("compaction lock unavailable, skipping compression: {}", exc)
            return request

    async def _aapply_compression(
        self,
        request: ModelRequest[ContextT],
        session_id: str,
    ) -> ModelRequest[ContextT]:
        """Async twin of _apply_compression: same lock, fail-open on timeout."""
        try:
            async with self._compaction_lock.acquire(session_id):
                return await self._aapply_compression_under_lock(request, session_id)
        except CompactionLockError as exc:
            logger.warning("compaction lock unavailable, skipping compression: {}", exc)
            return request

    def _apply_compression_under_lock(
        self,
        request: ModelRequest[ContextT],
        session_id: str,
    ) -> ModelRequest[ContextT]:
        original_messages: list[AnyMessage] = request.state.get("messages", [])
        recovery_ctx = self._capture_recovery_context(original_messages, session_id)

        current_messages, non_llm_reduced = self._run_non_llm_strategies(
            list(original_messages), session_id
        )
        strategy_used = "non_llm" if non_llm_reduced > 0 else None

        current_tokens = self._estimate_tokens(current_messages)
        skip_llm = state_register_mem.get_state(session_id, _SKIP_LLM_KEY, False)
        budget = self._calculate_preserve_budget()

        if current_tokens > budget * 2 or skip_llm or non_llm_reduced == 0:
            cutoff = self._determine_cutoff(current_messages)
            if cutoff > 0:
                messages_to_summarize = current_messages[:cutoff]
                preserved = current_messages[cutoff:]
                messages_to_summarize = offload_inline_media(messages_to_summarize, session_id)

                self._fire_compression_nudges(session_id, original_messages, messages_to_summarize)

                # Persist cross-session facts before these messages are discarded.
                if self._memory_store and self._llm_factory:
                    from .memory_flush import run_memory_flush_sync

                    est_tokens = self._estimate_tokens(messages_to_summarize)
                    run_memory_flush_sync(
                        messages_to_summarize,
                        est_tokens,
                        self._memory_store,
                        self._llm_factory,
                    )

                if skip_llm:
                    filtered = _filter_summary_messages(messages_to_summarize)
                    summary_text = _build_static_fallback_summary(filtered)
                    strategy_used = "fallback"
                else:
                    summary_text = self._create_summary(
                        messages_to_summarize, session_id=session_id
                    )
                    strategy_used = "llm_summary"

                new_messages = self._build_new_messages(summary_text)
                final_messages = [*new_messages, *preserved]
            else:
                final_messages = current_messages
                strategy_used = strategy_used or "noop"
        else:
            final_messages = current_messages
            strategy_used = strategy_used or "non_llm_sufficient"

        if self._estimate_tokens(final_messages) > budget * 2:
            final_messages = self._aggressive_truncate(final_messages)
            strategy_used = "aggressive"

        final_messages = self._truncate_summary_messages(final_messages)

        if recovery_ctx:
            final_messages = self._inject_recovery_context(final_messages, recovery_ctx, session_id)

        self._record_compression(session_id, original_messages, final_messages, strategy_used)
        self._compaction_just_happened = True
        self._compress_last_turn = False
        state_register_mem.set_state(session_id, _LAST_USER_QUESTION_KEY, "")

        system_prompt: str | None = None
        if self._need_update_system_prompt:
            system_prompt = self._rebuild_system_prompt(session_id)
            self._persist_system_prompt(session_id, system_prompt)

        override_kwargs: dict[str, Any] = {
            "messages": cast("list[AnyMessage]", final_messages),
        }
        if system_prompt:
            override_kwargs["system_message"] = SystemMessage(content=system_prompt)
        return request.override(**override_kwargs)

    async def _aapply_compression_under_lock(
        self,
        request: ModelRequest[ContextT],
        session_id: str,
    ) -> ModelRequest[ContextT]:
        original_messages: list[AnyMessage] = request.state.get("messages", [])
        recovery_ctx = self._capture_recovery_context(original_messages, session_id)

        current_messages, non_llm_reduced = self._run_non_llm_strategies(
            list(original_messages), session_id
        )
        strategy_used = "non_llm" if non_llm_reduced > 0 else None

        current_tokens = self._estimate_tokens(current_messages)
        skip_llm = state_register_mem.get_state(session_id, _SKIP_LLM_KEY, False)
        budget = self._calculate_preserve_budget()

        if current_tokens > budget * 2 or skip_llm or non_llm_reduced == 0:
            cutoff = self._determine_cutoff(current_messages)
            if cutoff > 0:
                messages_to_summarize = current_messages[:cutoff]
                preserved = current_messages[cutoff:]
                messages_to_summarize = offload_inline_media(messages_to_summarize, session_id)

                self._fire_compression_nudges(session_id, original_messages, messages_to_summarize)

                # Persist cross-session facts before these messages are discarded.
                if self._memory_store and self._llm_factory:
                    from .memory_flush import run_memory_flush

                    est_tokens = self._estimate_tokens(messages_to_summarize)
                    await run_memory_flush(
                        messages_to_summarize,
                        est_tokens,
                        self._memory_store,
                        self._llm_factory,
                    )

                if skip_llm:
                    filtered = _filter_summary_messages(messages_to_summarize)
                    summary_text = _build_static_fallback_summary(filtered)
                    strategy_used = "fallback"
                else:
                    summary_text = await self._acreate_summary(
                        messages_to_summarize, session_id=session_id
                    )
                    strategy_used = "llm_summary"

                new_messages = self._build_new_messages(summary_text)
                final_messages = [*new_messages, *preserved]
            else:
                final_messages = current_messages
                strategy_used = strategy_used or "noop"
        else:
            final_messages = current_messages
            strategy_used = strategy_used or "non_llm_sufficient"

        if self._estimate_tokens(final_messages) > budget * 2:
            final_messages = self._aggressive_truncate(final_messages)
            strategy_used = "aggressive"

        final_messages = self._truncate_summary_messages(final_messages)

        if recovery_ctx:
            final_messages = self._inject_recovery_context(final_messages, recovery_ctx, session_id)

        self._record_compression(session_id, original_messages, final_messages, strategy_used)
        self._compaction_just_happened = True
        self._compress_last_turn = False
        state_register_mem.set_state(session_id, _LAST_USER_QUESTION_KEY, "")

        system_prompt: str | None = None
        if self._need_update_system_prompt:
            system_prompt = self._rebuild_system_prompt(session_id)
            self._persist_system_prompt(session_id, system_prompt)

        override_kwargs: dict[str, Any] = {
            "messages": cast("list[AnyMessage]", final_messages),
        }
        if system_prompt:
            override_kwargs["system_message"] = SystemMessage(content=system_prompt)
        return request.override(**override_kwargs)

    def _determine_cutoff(self, messages: list[AnyMessage]) -> int:
        budget = self._calculate_preserve_budget()
        turns = split_into_turns(messages)

        total = 0
        cutoff = 0
        for turn in reversed(turns):
            size = self._estimate_tokens(turn.messages)
            if total + size <= budget:
                total += size
                cutoff = turn.start_idx
            else:
                remaining = budget - total
                split_idx = split_turn(turn, remaining, lambda msgs: self._estimate_tokens(msgs))
                if split_idx is not None:
                    cutoff = split_idx
                break

        cutoff = self._adjust_for_orphan_pairs(messages, cutoff)

        if not self._compress_last_turn:
            last_user_idx = next(
                (
                    i
                    for i in range(len(messages) - 1, -1, -1)
                    if isinstance(messages[i], HumanMessage)
                ),
                None,
            )
            if last_user_idx is not None and cutoff > last_user_idx:
                cutoff = last_user_idx

        return max(cutoff, 0)

    def _adjust_for_orphan_pairs(self, messages: list[AnyMessage], cutoff: int) -> int:
        return self._orphan_repairer.adjust(messages, cutoff)

    def _run_non_llm_strategies(
        self, messages: list[BaseMessage], session_id: str
    ) -> tuple[list[BaseMessage], int]:
        current = list(messages)
        total_reduced = 0

        current, reduced = dedup_tool_outputs(current, set(PROTECTED_TOOLS))
        total_reduced += reduced
        if reduced > 0:
            logger.debug("Dedup reduced ~{} tokens, session={}", reduced, session_id)

        current, reduced = prune_tool_outputs(
            current,
            protect_tokens=PRUNE_PROTECT_TOKENS,
            min_reduction_tokens=PRUNE_MIN_REDUCTION_TOKENS,
            protected_tools=set(PROTECTED_TOOLS),
        )
        total_reduced += reduced
        if reduced > 0:
            logger.debug("Prune reduced ~{} tokens, session={}", reduced, session_id)

        current_tokens = self._estimate_tokens(current)
        target = int(current_tokens * TARGET_TRUNCATE_RATIO)
        current, reduced = target_truncate_tool_outputs(
            current,
            target_reduction_tokens=target,
            min_output_chars=MIN_OUTPUT_CHARS_TO_TRUNCATE,
            max_output_chars=MAX_TOOL_OUTPUT_CHARS,
            protected_tools=set(PROTECTED_TOOLS),
        )
        total_reduced += reduced
        if reduced > 0:
            logger.debug("Target truncation reduced ~{} tokens, session={}", reduced, session_id)

        current, reduced = truncate_tool_args(
            current,
            max_args_chars=MAX_TOOL_ARGS_CHARS,
            min_args_chars=MIN_ARGS_CHARS_TO_TRUNCATE,
            protected_tools=set(PROTECTED_TOOLS),
        )
        total_reduced += reduced
        if reduced > 0:
            logger.debug("Tool args truncation reduced ~{} tokens, session={}", reduced, session_id)

        return current, total_reduced

    def _aggressive_truncate(self, messages: list[BaseMessage]) -> list[BaseMessage]:
        return self._truncator.aggressive_truncate(messages)

    def _capture_recovery_context(self, messages: list[BaseMessage], session_id: str) -> dict:
        last_human = next((m for m in reversed(messages) if isinstance(m, HumanMessage)), None)
        user_intent = ""
        if last_human and isinstance(last_human.content, str):
            user_intent = last_human.content[:LATEST_USER_REQUEST_MAX_CHARS]

        file_ops = _extract_file_operations(messages)
        previous_file_ops = state_register_mem.get_state(session_id, _PREVIOUS_FILE_OPS_KEY, None)
        state_register_mem.set_state(session_id, _PREVIOUS_FILE_OPS_KEY, file_ops)

        return {
            "user_intent": user_intent,
            "file_ops": file_ops,
            "previous_file_ops": previous_file_ops,
        }

    def _inject_recovery_context(
        self, messages: list[BaseMessage], ctx: dict, session_id: str
    ) -> list[BaseMessage]:
        file_ops_section = _format_file_ops(ctx.get("file_ops", {}), ctx.get("previous_file_ops"))

        for i, m in enumerate(messages):
            if (
                isinstance(m, AIMessage)
                and getattr(m, "additional_kwargs", {}).get("lc_source") == _SUMMARY_LC_SOURCE
            ):
                existing = m.content if isinstance(m.content, str) else str(m.content)
                pattern = r"## Relevant Files\n.*?(?=\n## |\n---|\n</summary>|\Z)"
                if re.search(pattern, existing, re.DOTALL):
                    replacement = f"## Relevant Files\n{file_ops_section}"
                    new_content = re.sub(pattern, replacement, existing, flags=re.DOTALL)
                else:
                    new_content = existing.replace(
                        _SUMMARY_CLOSE_TAG,
                        f"\n## Relevant Files\n{file_ops_section}\n{_SUMMARY_CLOSE_TAG}",
                    )
                kwargs = dict(m.additional_kwargs)
                raw_doc = kwargs.get("summary_doc")
                if isinstance(raw_doc, dict):
                    kwargs["summary_doc"] = {
                        **raw_doc,
                        "relevant_files": _labeled_file_ops(file_ops_section),
                    }
                messages[i] = m.model_copy(
                    update={"content": new_content, "additional_kwargs": kwargs}
                )
                break

        return messages

    @staticmethod
    def _truncate_content(content: str, max_chars: int) -> str:
        return MessageTruncator.truncate_content(content, max_chars)

    def _truncate_summary_messages(self, messages: list[BaseMessage]) -> list[BaseMessage]:
        return self._truncator.truncate_summary_messages(messages)

    def _preemptive_truncate(
        self, messages: list[BaseMessage], session_id: str
    ) -> list[BaseMessage]:
        return self._truncator.preemptive_truncate(messages, session_id)

    @staticmethod
    def _find_tool_name(messages: list[BaseMessage], tool_msg: ToolMessage, tc_id: str) -> str:
        return find_tool_name(messages, tool_msg, tc_id)
