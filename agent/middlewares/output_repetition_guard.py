"""Output repetition guard -- detects and breaks text-output death loops.

When the LLM falls into a loop where it:

1.  Repeats the **same text** across consecutive model calls
    (cross-call repetition), or
2.  Generates a **single response** containing the same phrase/line
    repeated many times (internal repetition),

this middleware detects the pattern and injects a warning or forces
a terminal stop.

Two escalation levels:

* **WARN** -- append a warning to the output, nudging the model to
  change strategy.
* **HALT** -- return a terminal ``AIMessage`` instructing the model to
  stop and summarize, preventing further wasted iterations.

This complements :class:`ToolGuardrails` (which detects tool-call
loops) by covering the case where the model loops on **text output**
without calling any tools.
"""

from __future__ import annotations
import hashlib
import re
from typing import Any, Callable, Awaitable

from loguru import logger
from langgraph.runtime import Runtime
from langgraph.typing import ContextT
from typing_extensions import override
from langchain_core.messages import AIMessage
from langchain.agents.middleware import AgentMiddleware, AgentState
from langchain.agents.middleware.types import (
    ResponseT,
    ModelRequest,
    ModelResponse,
    ExtendedModelResponse,
)

from runtime import state_register_mem

_HISTORY_KEY = "output_repetition_history"
_WARN_COUNT_KEY = "output_repetition_warn_count"
_INTERNAL_WARNED_KEY = "output_repetition_internal_warned"
_HALTED_KEY = "output_repetition_halted"
_REASONING_HISTORY_KEY = "output_repetition_reasoning_history"
_REASONING_WARNED_KEY = "output_repetition_reasoning_warned"

_MAX_HISTORY = 30
_TAIL_CHARS = 500
_MIN_CONTENT_LENGTH = 20
_CHAR_RUN_MIN = 8
_SENTENCE_SPLIT = re.compile(r"[。.!?！？\n]+")
_THINK_TAG = re.compile(r"", re.DOTALL)
_THINK_HTML = re.compile(r"<thinking>(.*?)</thinking>", re.DOTALL)

class OutputRepetitionGuard(AgentMiddleware):
    """Detect and break text-output death loops.

    Parameters
    ----------
    max_identical_outputs : int
        Maximum number of consecutive identical model outputs before a
        hard stop is triggered.  Default **3**.
    warn_after : int
        Number of consecutive identical outputs before a warning is
        appended to the result.  Default **2**.
    internal_repeat_ratio : float
        If more than this fraction of lines/sentences in a single output
        are duplicates, the output is considered internally repetitive.
        Default **0.6**.
    internal_min_lines : int
        Minimum number of non-empty lines/sentences in an output before
        line/sentence-level repetition detection kicks in.  Prevents
        false positives on short responses.  Default **6**.
    char_run_min : int
        Minimum consecutive occurrences of the same non-whitespace
        character to trigger character-run detection.  Catches patterns
        like ``啊啊啊啊啊啊啊啊``.  Default **8**.
    """

    def __init__(
            self,
        max_identical_outputs: int = 3,
        warn_after: int = 2,
        internal_repeat_ratio: float = 0.6,
        internal_min_lines: int = 6,
        char_run_min: int = _CHAR_RUN_MIN,
    ):
        super().__init__()
        self.max_identical_outputs = max_identical_outputs
        self.warn_after = warn_after
        self.internal_repeat_ratio = internal_repeat_ratio
        self.internal_min_lines = internal_min_lines
        self.char_run_min = char_run_min

    # ----- session helpers -----------------------------------------------
    def _get_session_id(self, state: AgentState) -> str:
        session_id: str = state.get("session_id", "")
        if not session_id.strip():
            raise RuntimeError("OutputRepetitionGuard: session_id is required")
        return session_id

    # ----- hashing helpers -----------------------------------------------
    @staticmethod
    def _content_hash(content: str) -> str:
        """Hash the tail of the content for cross-call comparison."""
        tail = content[-_TAIL_CHARS:] if len(content) > _TAIL_CHARS else content
        return hashlib.md5(tail.strip().encode()).hexdigest()

    # ----- internal repetition detection -----------------------------------------------
    def _detect_internal_repetition(self, content: str) -> bool:
        """Check if a single output contains repetitive patterns.

        Combines three sub-detectors:

        1. **Segment-level** -- split by punctuation (``。.!?！？``) and
            ``\\n``, duplicate ratio.  Since the split regex includes
            ``\\n``, this also covers line-level repetition.
        2. **Character-run** -- same non-whitespace character repeated
        ``char_run_min``+ times consecutively.
        3. **Phrase-periodic** -- a short substring (2-10 chars) repeated
            consecutively ``phrase_min_repeats``+ times with no delimiter.
            Catches ``我来帮你我来帮你我来帮你我来帮你我来帮你我来帮你``.
        Returns ``True`` if **any** sub-detector fires.
        """
        if self._detect_sentence_repetition(content):
            return True
        if self._detect_char_run(content):
            return True
        if self._detect_phrase_repetition(content):
            return True
        return False

    def _detect_sentence_repetition(self, content: str) -> bool:
        sentences = [s.strip() for s in _SENTENCE_SPLIT.split(content) if s.strip()]
        if len(sentences) < self.internal_min_lines:
            return False
        unique = len(set(sentences))
        ratio = 1.0 - (unique / len(sentences))
        return ratio > self.internal_repeat_ratio

    def _detect_char_run(self, content: str) -> bool:
        threshold = self.char_run_min - 1
        pattern = rf"([^\s])\1{{{threshold},}}"
        return bool(re.search(pattern, content))

    def _detect_phrase_repetition(
        self,
        content: str,
        min_repeats: int = 5,
        max_phrase: int = 10,
    ) -> bool:
        content = re.sub(r"[ \t]+", "", content)
        n = len(content)
        if n < 2 * min_repeats:
            return False

        upper = min(max_phrase, n // min_repeats)
        for plen in range(2, upper + 1):
            limit = n - plen * min_repeats
            for start in range(min(plen, limit + 1)):
                pattern = content[start:start + plen]
                if not pattern.strip():
                    continue
                repeats = 1
                pos = start + plen
                while pos + plen <= n and content[pos:pos + plen] == pattern:
                    repeats += 1
                    pos += plen
                if repeats >= min_repeats:
                    return True
        return False

    @staticmethod
    def _extract_ai_message(result: Any) -> AIMessage | None:
        if isinstance(result, AIMessage):
            return result
        if isinstance(result, ModelResponse):
            for msg in result.result:
                if isinstance(msg, AIMessage):
                    return msg
        if isinstance(result, ExtendedModelResponse):
            return OutputRepetitionGuard._extract_ai_message(result.model_response)
        return None

    @staticmethod
    def _extract_reasoning(msg: AIMessage) -> str:
        ak = getattr(msg, "additional_kwargs", None)
        if ak and isinstance(ak, dict):
            reasoning = str(ak.get("reasoning_content", "") or "").strip()
            if reasoning:
                return reasoning
            reasoning = str(ak.get("reasoning", "") or "").strip()
            if reasoning:
                return reasoning
        return ""

    @staticmethod
    def _extract_inline_reasoning(content: str) -> str:
        parts = [
            m.gro(1).strip()
            for m in _THINK_TAG.finditer(content)
            if m.group(1).strip()
        ]
        parts += [
            m.group(1).strip()
            for m in _THINK_HTML.finditer(content)
            if m.group(1).strip()
        ]
        return "\n".join(parts)

    @staticmethod
    def _strip_inline_reasoning(content: str) -> str:
        content = _THINK_TAG.sub("", content)
        content = _THINK_HTML.sub("", content)
        return content.strip()

    def _check_text_repetition(
        self,
        session_id: str,
        text: str,
        content_prefix: str,
        history_key: str,
        internal_warned_key: str,
        label: str,
    ) -> AIMessage | None:
        ch = self._content_hash(text)
        history: list[str] = state_register_mem.get_state(
            session_id, history_key, []
        )

        consecutive = 0
        for h in reversed(history):
            if h == ch:
                consecutive += 1
            else:
                break

        history.append(ch)
        if len(history) > _MAX_HISTORY:
            history = history[-_MAX_HISTORY:]
        state_register_mem.set_state(session_id, history_key, history)

        total_identical = consecutive + 1

        if total_identical >= self.max_identical_outputs:
            state_register_mem.set_state(session_id, _HALTED_KEY, True)
            logger.warning(
                "[OutputRepetitionGuard] session={} cross-call "
                "{} repetition detected (total_identical={}, max={}) "
                "-- forcing halt",
                session_id, label, total_identical, self.max_identical_outputs,
            )
            return AIMessage(
                content=(
                    f"[Output Repetition Guard] The same {label} has been "
                    f"repeated {total_identical} consecutive times. "
                    "I must stop here. Please summarize what has been "
                    "accomplished and what remains to be done."
                )
            )

        if total_identical >= self.warn_after:
            logger.debug(
                "[OutputRepetitionGuard] session={} cross-call "
                "{} repetition warning (total_identical={})",
                session_id, label, total_identical,
            )
            prefix = (content_prefix + "\n\n") if content_prefix else ""
            return AIMessage(
                content=prefix
                + f" [Output Repetition Guard] Detected {label} "
                f"repetition ({total_identical} times). Please change "
                f"your approach or provide a final answer."
            )

        if self._detect_internal_repetition(text):
            already_warned: bool = state_register_mem.get_state(
                session_id, internal_warned_key, False
            )
            if not already_warned:
                state_register_mem.set_state(
                    session_id, internal_warned_key, True
                )
                logger.debug(
                    "[OutputRepetitionGuard] session={} internal "
                    "{} repetition detected -- warning",
                    session_id, label,
                )
                prefix = (content_prefix + "\n\n") if content_prefix else ""
                return AIMessage(
                content=prefix
                    + f" [Output Repetition Guard] Your {label} "
                    "contains highly repetitive patterns. Please avoid "
                    "repeating the same content and provide a concise answer."
                )

        return None

    def _wrap_model_call_post(
        self,
        request: ModelRequest[ContextT],
        result: Any,
    ) -> AIMessage | None:
        session_id = self._get_session_id(request.state)

        ai_msg = self._extract_ai_message(result)
        if ai_msg is None:
            return None

        # Skip if model is making tool calls -- that's ToolGuardrails' job
        tool_calls = getattr(ai_msg, "tool_calls", None)
        if tool_calls:
            return None

        content = str(ai_msg.content or "").strip()
        reasoning = self._extract_reasoning(ai_msg)

        if not reasoning:
            reasoning = self._extract_inline_reasoning(content)
            if reasoning:
                content = self._strip_inline_reasoning(content)

        # If already halted this turn, keep returning the halt message
        halted: bool = state_register_mem.get_state(
            session_id, _HALTED_KEY, False
        )
        if halted:
            return AIMessage(
                content=(
                "[Output Repetition Guard] Output repetition was "
                "detected earlier this turn. I must stop here."
                )
            )

        if len(content) >= _MIN_CONTENT_LENGTH:
            r = self._check_text_repetition(
                session_id, content, content,
                _HISTORY_KEY, _INTERNAL_WARNED_KEY,
                "output",
            )
            if r is not None:
                return r

        if len(reasoning) >= _MIN_CONTENT_LENGTH:
            r = self._check_text_repetition(
                session_id, reasoning, content,
                _REASONING_HISTORY_KEY, _REASONING_WARNED_KEY,
                "reasoning",
            )

            if r is not None:
                return r

        return None

    def _before_agent_impl(self, state: AgentState) -> None:
        session_id = self._get_session_id(state)
        state_register_mem.set_state(session_id, _HISTORY_KEY, [])
        state_register_mem.set_state(session_id, _WARN_COUNT_KEY, 0)
        state_register_mem.set_state(session_id, _INTERNAL_WARNED_KEY, False)
        state_register_mem.set_state(session_id, _HALTED_KEY, False)
        state_register_mem.set_state(session_id, _REASONING_HISTORY_KEY, [])
        state_register_mem.set_state(session_id, _REASONING_WARNED_KEY, False)

    @override
    def before_agent(
        self, state: AgentState, runtime: Runtime[ContextT]
    ) -> dict[str, Any] | None:
        logger.debug("{} before_agent hook fired", type(self).__name__)
        self._before_agent_impl(state)
        return None

    @override
    async def abefore_agent(
        self, state: AgentState, runtime: Runtime[ContextT]
    ) -> dict[str, Any] | None:
        logger.debug("{} abefore_agent hook fired", type(self).__name__)
        self._before_agent_impl(state)
        return None

    @override
    def wrap_model_call(
        self,
        request: ModelRequest[ContextT],
        handler: Callable[[ModelRequest[ContextT]], ModelResponse[ResponseT]],
    ) -> ModelResponse[ResponseT] | AIMessage | ExtendedModelResponse[ResponseT]:
        logger.debug("{} wrap_model_call hook fired", type(self).__name__)
        result = handler(request)
        replacement = self._wrap_model_call_post(request, result)
        if replacement is not None:
            return replacement
        return result

    @override
    async def awrap_model_call(
        self,
        request: ModelRequest[ContextT],
        handler: Callable[[ModelRequest[ContextT]], Awaitable[ModelResponse[ResponseT]]],
    ) -> ModelResponse[ResponseT] | AIMessage | ExtendedModelResponse[ResponseT]:
        logger.debug("{} awrap_model_call hook fired", type(self).__name__)
        result = await handler(request)
        replacement = self._wrap_model_call_post(request, result)
        if replacement is not None:
            return replacement
        return result