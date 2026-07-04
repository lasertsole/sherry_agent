import math
from loguru import logger
from langgraph.runtime import Runtime
from langgraph.typing import ContextT
from typing_extensions import override
from langchain.agents import AgentState
from typing import Any, Callable, Awaitable
from langchain.agents.middleware.types import ResponseT
from workspace.prompt_builder import build_system_prompt
from runtime import state_register_db, state_register_mem
from langchain_core.messages import AnyMessage, BaseMessage, SystemMessage, AIMessage, HumanMessage, ToolMessage, RemoveMessage
from langchain.agents.middleware import SummarizationMiddleware, ModelRequest, ModelResponse, ExtendedModelResponse


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
    def __init__(self, **kwargs):
        super().__init__(**kwargs)

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

    @override
    async def abefore_agent(
        self, state: AgentState, runtime: Runtime[ContextT]
    ) -> dict[str, Any] | None:
        session_id: str = state.get("session_id", "")
        if session_id.strip():
            state_register_mem.set_state(session_id, _COMPRESSION_COUNT_KEY, 0)
            state_register_mem.set_state(session_id, _COMPRESSION_INEFFECTIVE_KEY, 0)
            state_register_mem.set_state(session_id, _COMPRESSION_LAST_TOKENS_KEY, None)

    @override
    async def abefore_model(
        self, state: AgentState[Any], runtime: Runtime[ContextT]
    ) -> dict[str, Any] | None:
        pass

    @staticmethod
    def _is_output_cap_error(error: Exception) -> bool:
        msg = str(error).lower()
        return any(kw in msg for kw in ("max_tokens", "output length", "output cap", "max output"))

    @staticmethod
    def _should_skip_compression(session_id: str) -> bool:
        attempts: int = state_register_mem.get_state(session_id, _COMPRESSION_COUNT_KEY, 0)
        if attempts >= _MAX_COMPRESSION_ATTEMPTS:
            logger.info(
                "Summarization: max compression attempts ({}) reached for session {}.",
                _MAX_COMPRESSION_ATTEMPTS, session_id,
            )
            return True

        ineffective: int = state_register_mem.get_state(session_id, _COMPRESSION_INEFFECTIVE_KEY, 0)
        if ineffective >= _INEFFECTIVE_THRESHOLD:
            logger.info(
                "Summarization: anti-thrashing triggered (%d ineffective) for session %s.",
                ineffective, session_id,
            )
            return True

        return False

    def _record_compression(
        self, session_id: str, before_messages: list[BaseMessage], after_messages: list[BaseMessage],
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
            logger.info(
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

    @override
    async def awrap_model_call(
        self,
        request: ModelRequest[ContextT],
        handler: Callable[[ModelRequest[ContextT]], Awaitable[ModelResponse[ResponseT]]],
    ) -> ModelResponse[ResponseT] | AIMessage | ExtendedModelResponse[ResponseT]:
        state = request.state
        session_id = state.get("session_id", "")
        if session_id.strip() == "":
            err_text: str = "Not pass session_id"
            logger.error(err_text)
            raise RuntimeError(err_text)

        res: dict[str, Any] | None = await super().abefore_model(state, request.runtime)

        if res is None:
            return await handler(request)

        if self._should_skip_compression(session_id):
            return await handler(request)

        reduce_messages: list[BaseMessage] = [m for m in res["messages"] if not isinstance(m, RemoveMessage)]
        reduce_messages = self._truncate_messages(reduce_messages)
        original_messages: list[BaseMessage] = state.get("messages", [])

        self._record_compression(session_id, original_messages, reduce_messages)

        from agent.tools import memory_store
        memory_store.load_from_disk()

        system_prompt: str = build_system_prompt()
        state_register_mem.set_state(session_id, "system_prompt", system_prompt)
        state_register_db.set_state(session_id, "system_prompt", system_prompt)

        return await handler(
            request.override(messages = reduce_messages, system_message = SystemMessage(content = system_prompt))
        )
