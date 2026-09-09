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

Detection primitives live in :mod:`agent.middlewares.repetition_detectors`;
session state handling lives in :mod:`agent.middlewares.repetition_state`.
This module is the middleware shell: escalation policy + hooks.
"""

from __future__ import annotations

from typing import Any
from collections.abc import Callable, Awaitable

from loguru import logger
from langgraph.typing import ContextT
from typing import override
from langchain_core.messages import AIMessage
from langchain.agents.middleware import AgentMiddleware, AgentState
from langchain.agents.middleware.types import (
    ResponseT,
    ModelRequest,
    ModelResponse,
    ExtendedModelResponse,
)

from agent.middlewares.base import BeforeAgentHooksMixin
from agent.middlewares.repetition_detectors import (
    _CHAR_RUN_MIN,
    _TAIL_CHARS as _TAIL_CHARS,
    CharRunDetector,
    PhraseRepetitionDetector,
    SentenceRepetitionDetector,
    content_hash,
    extract_inline_reasoning,
    extract_reasoning,
    normalize_for_hash,
    strip_inline_reasoning,
)
from agent.middlewares.repetition_state import (
    SESSION_STATE_KEYS as SESSION_STATE_KEYS,
    _HALTED_KEY as _HALTED_KEY,
    _MAX_HISTORY as _MAX_HISTORY,
    _WARN_COUNT_KEY as _WARN_COUNT_KEY,
    _HISTORY_KEY,
    _INTERNAL_WARNED_KEY,
    _REASONING_HISTORY_KEY,
    _REASONING_WARNED_KEY,
    RepetitionState,
)
from runtime import state_register_mem

# Minimum content/reasoning length before repetition detection runs at all,
# preventing false positives on short responses.
_MIN_CONTENT_LENGTH = 20
# Minimum content length for **cross-call** repetition detection.  Much lower
# than ``_MIN_CONTENT_LENGTH`` because even a single short sentence repeated
# across consecutive model calls is a valid death-loop signal.  Only
# non-empty content (>= 1 char) is required.
_MIN_CROSSCALL_LENGTH = 1


class OutputRepetitionGuard(BeforeAgentHooksMixin, AgentMiddleware):
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
        like ``aaaaaaaa``.  Default **8**.
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

        self._sentence_detector = SentenceRepetitionDetector(
            internal_repeat_ratio, internal_min_lines
        )
        self._char_run_detector = CharRunDetector(char_run_min)
        self._phrase_detector = PhraseRepetitionDetector()
        self._state = RepetitionState(state_register_mem)

    # ----- session helpers -----------------------------------------------
    def _get_session_id(self, state: AgentState) -> str:
        """Resolve and validate the session identifier for this state.

        Every per-session counter/flag stored by this middleware is keyed by
        this id, so it must always be present.

        Raises
        ------
        RuntimeError
            If ``session_id`` is missing or blank in ``state``.
        """
        session_id: str = state.get("session_id", "")
        if not session_id.strip():
            raise RuntimeError("OutputRepetitionGuard: session_id is required")
        return session_id

    # ----- hashing helpers -----------------------------------------------
    @staticmethod
    def _normalize_for_hash(content: str) -> str:
        """Normalize content for robust cross-call hash comparison."""
        return normalize_for_hash(content)

    @staticmethod
    def _content_hash(content: str) -> str:
        """Hash content for cross-call comparison (dual head/tail hash)."""
        return content_hash(content)

    # ----- internal repetition detection ---------------------------------
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
            Catches ``I can helpI can helpI can helpI can helpI can helpI can help``.
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
        """Segment-level sub-detector: duplicate ratio of sentences/lines."""
        return self._sentence_detector.detect(content)

    def _detect_char_run(self, content: str) -> bool:
        """Character-run sub-detector: same char repeated consecutively."""
        return self._char_run_detector.detect(content)

    def _detect_phrase_repetition(
        self,
        content: str,
        min_repeats: int = 5,
        max_phrase: int = 10,
    ) -> bool:
        """Phrase-periodic sub-detector: short substring repeated back-to-back."""
        return self._phrase_detector.detect(content, min_repeats, max_phrase)

    @staticmethod
    def _extract_ai_message(result: Any) -> AIMessage | None:
        """Pull the first ``AIMessage`` out of any result shape the pipeline can
        produce -- a bare message, a :class:`ModelResponse`, or a nested
        :class:`ExtendedModelResponse`.
        """
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
        """Extract explicit reasoning text stored on ``additional_kwargs``."""
        return extract_reasoning(msg)

    @staticmethod
    def _extract_inline_reasoning(content: str) -> str:
        """Extract reasoning text wrapped in inline CoT tags (``<think>`` etc.)."""
        return extract_inline_reasoning(content)

    @staticmethod
    def _strip_inline_reasoning(content: str) -> str:
        """Remove inline CoT tags (``<think>`` etc.) from visible content."""
        return strip_inline_reasoning(content)

    def _check_text_repetition(
        self,
        session_id: str,
        text: str,
        content_prefix: str,
        history_key: str,
        internal_warned_key: str,
        label: str,
        check_internal: bool = True,
    ) -> AIMessage | None:
        """Shared cross-call + internal repetition check for any text stream.

        Used both for the visible output and for reasoning text, parametrised
        by the state keys and a ``label`` used in log/message text.

        Parameters
        ----------
        check_internal : bool
            Whether to run the internal-repetition sub-detectors on ``text``.
            Set to ``False`` when the model is making tool calls (text-output
            guard only) to avoid false positives on tool-call accompanying
            text.  Cross-call detection always runs regardless.

        Escalation ladder (checked in order):

        1. **HALT** -- ``max_identical_outputs`` consecutive identical hashes.
           Returns a terminal ``AIMessage`` ordering the model to stop and set
           ``_HALTED_KEY`` so the halt is sticky for the rest of the turn.
        2. **WARN** -- ``warn_after`` consecutive identical hashes.  Returns an
           ``AIMessage`` nudging the model to change strategy.
        3. **INTERNAL WARN (once)** -- the current text is internally repetitive
           (see :func:`_detect_internal_repetition`) and no such warning has
           fired yet for this session/label.

        Returns ``None`` if no escalation applies.
        """
        ch = self._content_hash(text)
        # Dual hash: "head_hash|tail_hash".  Split into parts for comparison.
        ch_head, _, ch_tail = ch.partition("|")

        # Rolling history of content hashes; ``consecutive`` is the length of
        # the run of hashes matching the current one at the tail of the list.
        # A previous entry matches if EITHER its head or tail hash equals the
        # current head or tail hash respectively -- this catches repetition
        # at either end of long outputs.
        history = self._state.get_history(session_id, history_key)

        consecutive = 0
        for h in reversed(history):
            h_head, _, h_tail = h.partition("|")
            if ch_head == h_head or ch_tail == h_tail:
                consecutive += 1
            else:
                break

        self._state.record_hash(session_id, history_key, ch)

        # This occurrence itself counts toward the run.
        total_identical = consecutive + 1

        # ---- 1. Hard halt ----------------------------------------------
        if total_identical >= self.max_identical_outputs:
            self._state.mark_halted(session_id)
            logger.warning(
                "[OutputRepetitionGuard] session={} cross-call "
                "{} repetition detected (total_identical={}, max={}) "
                "-- forcing halt",
                session_id,
                label,
                total_identical,
                self.max_identical_outputs,
            )
            return AIMessage(
                content=(
                    f"[Output Repetition Guard] The same {label} has been "
                    f"repeated {total_identical} consecutive times. "
                    "I must stop here. Please summarize what has been "
                    "accomplished and what remains to be done."
                )
            )

        # ---- 2. Soft warning --------------------------------------------
        if total_identical >= self.warn_after:
            logger.debug(
                "[OutputRepetitionGuard] session={} cross-call "
                "{} repetition warning (total_identical={})",
                session_id,
                label,
                total_identical,
            )
            # Keep the model's own text so we only *nudge*, never erase it.
            prefix = (content_prefix + "\n\n") if content_prefix else ""
            return AIMessage(
                content=prefix + f" [Output Repetition Guard] Detected {label} "
                f"repetition ({total_identical} times). Please change "
                f"your approach or provide a final answer."
            )

        # ---- 3. Internal repetition (at most once per session/label) ----
        if check_internal and self._detect_internal_repetition(text):
            if not self._state.is_warned(session_id, internal_warned_key):
                self._state.mark_warned(session_id, internal_warned_key)
                logger.debug(
                    "[OutputRepetitionGuard] session={} internal {} repetition detected -- warning",
                    session_id,
                    label,
                )
                prefix = (content_prefix + "\n\n") if content_prefix else ""
                return AIMessage(
                    content=prefix + f" [Output Repetition Guard] Your {label} "
                    "contains highly repetitive patterns. Please avoid "
                    "repeating the same content and provide a concise answer."
                )

        return None

    def _wrap_model_call_post(
        self,
        request: ModelRequest[ContextT],
        result: Any,
    ) -> AIMessage | None:
        """Post-hoc inspection of a single model call's result.

        Extracts the produced ``AIMessage`` (plus reasoning), then routes it
        through :func:`_check_text_repetition` for both the visible content and
        the reasoning text independently.  Returns a replacement ``AIMessage``
        when an escalation should override the model's output, otherwise
        ``None``.
        """
        session_id = self._get_session_id(request.state)

        ai_msg = self._extract_ai_message(result)
        if ai_msg is None:
            return None

        # Skip internal completion-drain messages (subagent completion
        # notifications injected by SubagentCompletionDrainMiddleware).
        _meta = getattr(ai_msg, "metadata", None)
        if isinstance(_meta, dict):
            if _meta.get("internal") is True:
                return None
            if _meta.get("provenance") == "subagent_completion":
                return None

        # Don't skip entirely when tool_calls are present.  Cross-call
        # repetition detection still runs (the model may loop on the same
        # text alongside tool calls).  Only internal-repetition detection
        # is skipped for tool-call messages to avoid false positives.
        has_tool_calls = bool(getattr(ai_msg, "tool_calls", None))

        content = str(ai_msg.content or "").strip()
        reasoning = self._extract_reasoning(ai_msg)

        # Fall back to inline <think>...</think> style reasoning: extract it and
        # strip the tags from the visible content before detection.
        if not reasoning:
            reasoning = self._extract_inline_reasoning(content)
            if reasoning:
                content = self._strip_inline_reasoning(content)

        # If already halted this turn, keep returning the halt message
        if self._state.is_halted(session_id):
            return AIMessage(
                content=(
                    "[Output Repetition Guard] Output repetition was "
                    "detected earlier this turn. I must stop here."
                )
            )

        # Cross-call detection uses _MIN_CROSSCALL_LENGTH (1) so even
        # short repeated outputs are caught.  Internal detection uses the
        # higher _MIN_CONTENT_LENGTH threshold and is skipped when the
        # model is making tool calls.
        if len(content) >= _MIN_CROSSCALL_LENGTH:
            r = self._check_text_repetition(
                session_id,
                content,
                content,
                _HISTORY_KEY,
                _INTERNAL_WARNED_KEY,
                "output",
                check_internal=(len(content) >= _MIN_CONTENT_LENGTH and not has_tool_calls),
            )
            if r is not None:
                return r

        # Reasoning streams are checked independently and share the visible
        # content as the ``content_prefix`` so a warning keeps context.
        if len(reasoning) >= _MIN_CROSSCALL_LENGTH:
            r = self._check_text_repetition(
                session_id,
                reasoning,
                content,
                _REASONING_HISTORY_KEY,
                _REASONING_WARNED_KEY,
                "reasoning",
                check_internal=(len(reasoning) >= _MIN_CONTENT_LENGTH and not has_tool_calls),
            )

            if r is not None:
                return r

        return None

    def _before_agent_impl(self, state: AgentState) -> None:
        """Reset all per-session repetition state at the start of each turn."""
        self._state.reset(self._get_session_id(state))

    @override
    def wrap_model_call(
        self,
        request: ModelRequest[ContextT],
        handler: Callable[[ModelRequest[ContextT]], ModelResponse[ResponseT]],
    ) -> ModelResponse[ResponseT] | AIMessage | ExtendedModelResponse[ResponseT]:
        """Synchronous wrapper around every model call.

        Invokes the wrapped ``handler``, then inspects the result via
        :func:`_wrap_model_call_post`.  Returns the hijack ``AIMessage`` when a
        repetition escalation fires, otherwise the original result unchanged.
        """
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
        """Async wrapper around every model call.

        Same behaviour as :func:`wrap_model_call` but awaits the async
        ``handler`` first.
        """
        logger.debug("{} awrap_model_call hook fired", type(self).__name__)
        result = await handler(request)
        replacement = self._wrap_model_call_post(request, result)
        if replacement is not None:
            return replacement
        return result


# ---------------------------------------------------------------------------
# Stream-layer (Layer C) helper.
#
# The middleware backstop (``wrap_model_call``) inspects a model call's result
# *after* the full response is produced -- a post-hoc replacement. During
# streaming the client already received the repetitive text before it can be
# replaced. This helper lets the stream-consumption loop (see
# ``server/service/messages.py::async_generate``) run the same internal
# repetition detectors on the *accumulated visible text as it streams*, so the
# repetitive tail is cut before it reaches the client.
#
# It reuses a lightweight shared instance with the middleware's default
# thresholds and the same ``_INTERNAL_WARNED_KEY`` dedupe gate, so a session
# warns at most once across both layers.
# ---------------------------------------------------------------------------
_STREAM_GUARD = OutputRepetitionGuard()

# Warning surfaced to the user on a stream-cut, mirroring the middleware's
# internal-repetition wording so it carries the same ``[Output Repetition
# Guard]`` marker the frontend recognizes as guard output.
_STREAM_WARNING = (
    " [Output Repetition Guard] Your output contains highly repetitive "
    "patterns. Please avoid repeating the same content and provide a concise "
    "answer."
)


def check_stream_repetition(session_id: str, accumulated_text: str) -> str | None:
    """Stream-level (Layer C) internal-repetition check.

    Runs the internal-repetition sub-detectors on ``accumulated_text`` (the
    visible model text gathered so far in the current stream). When a
    repetitive pattern is detected **and** no internal-repetition warning has
    fired for this session yet, marks the shared ``_INTERNAL_WARNED_KEY`` and
    returns a warning string for the caller to yield in place of the remaining
    repetitive stream.

    Honors the same ``_INTERNAL_WARNED_KEY`` dedupe gate as the middleware
    ``wrap_model_call`` path, so a session warns at most once across both the
    streaming path and the post-hoc backstop.

    Returns ``None`` when no escalation applies.
    """
    # Skip detection entirely below the content floor, mirroring the middleware
    # behaviour -- this avoids churn on tiny fragments and keeps the
    # false-positive surface identical to the post-hoc path.
    if len(accumulated_text) < _MIN_CONTENT_LENGTH:
        return None

    if not _STREAM_GUARD._detect_internal_repetition(accumulated_text):
        return None

    if state_register_mem.get_state(session_id, _INTERNAL_WARNED_KEY, False):
        return None

    state_register_mem.set_state(session_id, _INTERNAL_WARNED_KEY, True)
    logger.debug(
        "[OutputRepetitionGuard] session={} stream internal repetition detected -- cutting output",
        session_id,
    )
    return _STREAM_WARNING
