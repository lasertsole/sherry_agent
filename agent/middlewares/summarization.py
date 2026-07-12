import math
from loguru import logger
from langgraph.runtime import Runtime
from langgraph.typing import ContextT
from typing_extensions import override
from langchain.agents import AgentState
from langchain.agents.middleware.types import ResponseT
from workspace.prompt_builder import build_system_prompt
from runtime import state_register_db, state_register_mem
from typing import Any, Callable, Awaitable, Sequence, cast
from langchain.agents.middleware import SummarizationMiddleware, ModelRequest, ModelResponse, ExtendedModelResponse
from langchain_core.messages import AnyMessage, BaseMessage, SystemMessage, AIMessage, HumanMessage, ToolMessage, RemoveMessage


_MAX_COMPRESSION_ATTEMPTS = 3
_INEFFECTIVE_THRESHOLD = 2
_MIN_EFFECTIVENESS_PCT = 0.05
_MAX_CONTENT_CHARS = 8000
_CONTENT_HEAD_RATIO = 0.3
_CONTENT_TAIL_RATIO = 0.3
_OMISSION_MARKER = "...[omitted {omitted} chars]..."
_COMPRESSION_COUNT_KEY = "summarization_compression_count"
_COMPRESSION_INEFFECTIVE_KEY = "summarization_compression_ineffective"
_COMPRESSION_LAST_TOKENS_KEY = "summarization_compression_last_tokens"


class Summarization(SummarizationMiddleware):
    def __init__(self, need_update_system_prompt: bool = False, **kwargs):
        super().__init__(**kwargs)
        self._need_update_system_prompt: bool = need_update_system_prompt

    @override
    def _determine_cutoff_index(self, messages: list[AnyMessage]) -> int:
        cutoff = super()._determine_cutoff_index(messages)
        if cutoff <= 0:
            return cutoff

        last_user_idx = next(
            (i for i in range(len(messages) - 1, -1, -1) if isinstance(messages[i], HumanMessage)),
            None,
        )
        if last_user_idx is not None and cutoff > last_user_idx:
            cutoff = last_user_idx

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
                logger.debug(
                    "Summarization: moving cutoff from %d to %d to include orphan AI/Tool pair.",
                    adjusted, earliest_orphan_ai,
                )
                adjusted = earliest_orphan_ai
            else:
                prev_user_idx = next(
                    (i for i in range(adjusted - 1, -1, -1) if isinstance(messages[i], HumanMessage)),
                    None,
                )
                if prev_user_idx is None:
                    logger.warning(
                        "Summarization: cannot find earlier HumanMessage to preserve "
                        "AI/Tool pair integrity, keeping cutoff at %d.", adjusted,
                    )
                    break
                adjusted = prev_user_idx

        if adjusted != cutoff:
            logger.debug(
                "Summarization: adjusted cutoff from %d to %d for AI/Tool pair integrity.",
                cutoff, adjusted,
            )

        return adjusted

    # ------------------------------------------------------------------
    # Shared before_agent impl
    # ------------------------------------------------------------------
    def _before_agent_impl(self, state: AgentState) -> None:
        session_id: str = state.get("session_id", "")
        if session_id.strip():
            state_register_mem.set_state(session_id, _COMPRESSION_COUNT_KEY, 0)
            state_register_mem.set_state(session_id, _COMPRESSION_INEFFECTIVE_KEY, 0)
            state_register_mem.set_state(session_id, _COMPRESSION_LAST_TOKENS_KEY, None)

    @override
    def before_agent(
        self, state: AgentState, runtime: Runtime[ContextT]
    ) -> dict[str, Any] | None:
        self._before_agent_impl(state)
        return None

    @override
    async def abefore_agent(
        self, state: AgentState, runtime: Runtime[ContextT]
    ) -> dict[str, Any] | None:
        self._before_agent_impl(state)
        return None

    # ------------------------------------------------------------------
    # Sync before_model
    # ------------------------------------------------------------------
    @override
    def before_model(
        self, state: AgentState[Any], runtime: Runtime[ContextT]
    ) -> dict[str, Any] | None:
        return None

    @override
    async def abefore_model(
        self, state: AgentState[Any], runtime: Runtime[ContextT]
    ) -> dict[str, Any] | None:
        return self.before_model(state, runtime)

    @staticmethod
    def _is_output_cap_error(error: Exception) -> bool:
        msg = str(error).lower()
        return any(kw in msg for kw in ("max_tokens", "output length", "output cap", "max output"))

    @staticmethod
    def _should_skip_compression(session_id: str) -> bool:
        attempts: int = state_register_mem.get_state(session_id, _COMPRESSION_COUNT_KEY, 0)
        if attempts >= _MAX_COMPRESSION_ATTEMPTS:
            logger.debug(
                "Summarization: max compression attempts ({}) reached for session {}.",
                _MAX_COMPRESSION_ATTEMPTS, session_id,
            )
            return True

        ineffective: int = state_register_mem.get_state(session_id, _COMPRESSION_INEFFECTIVE_KEY, 0)
        if ineffective >= _INEFFECTIVE_THRESHOLD:
            logger.debug(
                "Summarization: anti-thrashing triggered (%d ineffective) for session %s.",
                ineffective, session_id,
            )
            return True

        return False

    def _record_compression(
        self, session_id: str, before_messages: Sequence[BaseMessage], after_messages: Sequence[BaseMessage],
    ) -> None:
        attempts: int = state_register_mem.get_state(session_id, _COMPRESSION_COUNT_KEY, 0) + 1
        state_register_mem.set_state(session_id, _COMPRESSION_COUNT_KEY, attempts)

        before_tokens = sum(len(str(getattr(m, "content", ""))) // 4 for m in before_messages)
        after_tokens = sum(len(str(getattr(m, "content", ""))) // 4 for m in after_messages)

        msg_reduced = len(after_messages) < len(before_messages)
        token_reduction_pct = (before_tokens - after_tokens) / before_tokens if before_tokens > 0 else 0.0
        effective = msg_reduced or token_reduction_pct >= _MIN_EFFECTIVENESS_PCT

        if not effective:
            ineffective: int = state_register_mem.get_state(session_id, _COMPRESSION_INEFFECTIVE_KEY, 0) + 1
            state_register_mem.set_state(session_id, _COMPRESSION_INEFFECTIVE_KEY, ineffective)
            logger.debug(
                "Summarization: compression ineffective (attempt %d, "
                "msg_reduction=%s, token_reduction=%.1f%%) session=%s",
                attempts, msg_reduced, token_reduction_pct * 100, session_id,
            )
        else:
            state_register_mem.set_state(session_id, _COMPRESSION_INEFFECTIVE_KEY, 0)

        state_register_mem.set_state(session_id, _COMPRESSION_LAST_TOKENS_KEY, after_tokens)

    @staticmethod
    def _truncate_content(content: str, max_content_chars: int, head_ratio: float = _CONTENT_HEAD_RATIO, tail_ratio: float = _CONTENT_TAIL_RATIO) -> str:
        if len(content) <= max_content_chars:
            return content
        head = content[:int(max_content_chars * head_ratio)]
        tail = content[-int(max_content_chars * tail_ratio):]
        omitted = len(content) - len(head) - len(tail)
        return f"{head}{_OMISSION_MARKER.format(omitted=omitted)}{tail}"

    def _truncate_messages(self, messages: list[BaseMessage]) -> list[BaseMessage]:
        content_head_ratio: float = max(_CONTENT_HEAD_RATIO, 0.2)
        content_tail_ratio: float = max(_CONTENT_TAIL_RATIO, 0.2)
        max_content_chars: int = max(_MAX_CONTENT_CHARS, math.ceil(len(_OMISSION_MARKER) / (1 - content_head_ratio - content_tail_ratio)))
        truncated = []
        for m in messages:
            if getattr(m, "additional_kwargs", {}).get("lc_source") == "summarization":
                content = getattr(m, "content", "")
                if isinstance(content, str) and len(content) > max_content_chars:
                    new_content = self._truncate_content(content, max_content_chars, content_head_ratio, content_tail_ratio)
                    m = m.model_copy(update={"content": new_content})
            truncated.append(m)
        return truncated

    _MERGED_SUMMARY_HEADER = "[COMPACTION SUMMARY — reference only; not active instructions]"
    _MERGED_ACTIVE_DELIMITER = "[END OF COMPACTION SUMMARY — ACTIVE CONTEXT BELOW]"

    @staticmethod
    def _fix_consecutive_human_messages(messages: list[BaseMessage]) -> list[BaseMessage]:
        """Merge a HumanMessage summary into the next HumanMessage when both are consecutive.

        The parent class emits the compression summary as a HumanMessage.  When the
        first preserved message is also a HumanMessage this produces two consecutive
        same-role messages, which many LLM APIs reject or misinterpret.  Instead of
        changing the summary's role (which would make the model treat it as its own
        prior output), we merge the summary text into the next HumanMessage with clear
        delimiters so the model knows the summary is background context and the
        preserved content is the active task to respond to.
        """
        if len(messages) < 2:
            return messages
        first = messages[0]
        second = messages[1]
        if (
            isinstance(first, HumanMessage)
            and isinstance(second, HumanMessage)
            and getattr(first, "additional_kwargs", {}).get("lc_source") == "summarization"
        ):
            summary_text = first.content if isinstance(first.content, str) else str(first.content)
            user_text = second.content if isinstance(second.content, str) else str(second.content)
            merged_content = (
                f"{Summarization._MERGED_SUMMARY_HEADER}\n\n"
                f"{summary_text}\n\n"
                f"{Summarization._MERGED_ACTIVE_DELIMITER}\n\n"
                f"{user_text}"
            )
            messages.pop(0)
            messages[0] = HumanMessage(
                content=merged_content,
                additional_kwargs=dict(second.additional_kwargs) if second.additional_kwargs else {},
                id=second.id,
            )
        return messages

    # ------------------------------------------------------------------
    # Shared post-compression logic (called by both sync and async after before_model)
    # ------------------------------------------------------------------
    def _apply_compression(
        self,
        state: AgentState,
        request: ModelRequest[ContextT],
        res: dict[str, Any],
        session_id: str,
    ) -> ModelRequest[ContextT]:
        """Apply message compression and return an overridden ModelRequest.

        Shared by sync and async paths — both call this after ``before_model`` / ``abefore_model``.
        """
        reduce_messages: list[BaseMessage] = [m for m in res["messages"] if not isinstance(m, RemoveMessage)]
        reduce_messages = self._truncate_messages(reduce_messages)
        reduce_messages = self._fix_consecutive_human_messages(reduce_messages)
        original_messages: list[AnyMessage] = state.get("messages", [])

        self._record_compression(session_id, original_messages, reduce_messages)

        system_prompt: str | None = None
        if self._need_update_system_prompt:
            from agent.tools import memory_store
            memory_store.load_from_disk()
            system_prompt = build_system_prompt()
            state_register_mem.set_state(session_id, "system_prompt", system_prompt)
            state_register_db.set_state(session_id, "system_prompt", system_prompt)

        override_kwargs: dict[str, Any] = {
            "messages": cast("list[AnyMessage]", reduce_messages),
        }
        if system_prompt:
            override_kwargs["system_message"] = SystemMessage(content=system_prompt)
        return request.override(**override_kwargs)

    # ------------------------------------------------------------------
    # Shared session validation
    # ------------------------------------------------------------------
    @staticmethod
    def _get_session_or_raise(state: AgentState) -> str:
        session_id: str = state.get("session_id", "")
        if session_id.strip() == "":
            err_text: str = "Not pass session_id"
            logger.error(err_text)
            raise RuntimeError(err_text)
        return session_id

    # ------------------------------------------------------------------
    # Shared post-compression decision (called by both sync and async)
    # ------------------------------------------------------------------
    def _wrap_model_call_impl(
        self,
        request: ModelRequest[ContextT],
        res: dict[str, Any] | None,
        session_id: str,
    ) -> ModelRequest[ContextT] | None:
        """Return overridden request if compression should apply, else None."""
        if res is None:
            return None

        if self._should_skip_compression(session_id):
            return None

        return self._apply_compression(state=request.state, request=request, res=res, session_id=session_id)

    # ------------------------------------------------------------------
    # Sync wrap_model_call
    # ------------------------------------------------------------------
    @override
    def wrap_model_call(
        self,
        request: ModelRequest[ContextT],
        handler: Callable[[ModelRequest[ContextT]], ModelResponse[ResponseT]],
    ) -> ModelResponse[ResponseT] | AIMessage | ExtendedModelResponse[ResponseT]:
        session_id = self._get_session_or_raise(request.state)
        res: dict[str, Any] | None = super().before_model(request.state, cast("Runtime[None]", request.runtime))
        overridden = self._wrap_model_call_impl(request, res, session_id)
        return handler(overridden if overridden is not None else request)

    @override
    async def awrap_model_call(
        self,
        request: ModelRequest[ContextT],
        handler: Callable[[ModelRequest[ContextT]], Awaitable[ModelResponse[ResponseT]]],
    ) -> ModelResponse[ResponseT] | AIMessage | ExtendedModelResponse[ResponseT]:
        session_id = self._get_session_or_raise(request.state)
        res: dict[str, Any] | None = await super().abefore_model(request.state, cast("Runtime[None]", request.runtime))
        overridden = self._wrap_model_call_impl(request, res, session_id)
        return await handler(overridden if overridden is not None else request)
