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

# ---------------------------------------------------------------------------
# Per-session state keys (stored in ``state_register_mem``).
#
# * ``_HISTORY_KEY``           -- rolling hashes of recent *visible output*
#   contents, used for cross-call identical-output detection.
# * ``_WARN_COUNT_KEY``        -- (reserved) cumulative warning counter; reset
#   at the start of every agent turn by ``_before_agent_impl``.
# * ``_INTERNAL_WARNED_KEY``   -- whether an internal-repetition warning has
#   already been issued for the *visible output* this session, so we warn at
#   most once.
# * ``_HALTED_KEY``            -- whether a hard HALT was already forced this
#   turn; when set, every subsequent model call returns the halt message.
# * ``_REASONING_HISTORY_KEY`` -- like ``_HISTORY_KEY`` but for *reasoning*
#   text (CoT / ``<think>`` chains) so output and reasoning loops are tracked
#   independently.
# * ``_REASONING_WARNED_KEY``  -- like ``_INTERNAL_WARNED_KEY`` but scoped to
#   reasoning text.
# ---------------------------------------------------------------------------
_HISTORY_KEY = "output_repetition_history"
_WARN_COUNT_KEY = "output_repetition_warn_count"
_INTERNAL_WARNED_KEY = "output_repetition_internal_warned"
_HALTED_KEY = "output_repetition_halted"
_REASONING_HISTORY_KEY = "output_repetition_reasoning_history"
_REASONING_WARNED_KEY = "output_repetition_reasoning_warned"

# The 6 per-session state keys owned by this middleware, exposed publicly so
# the subagent teardown path can release exactly these (and only these) keys
# via ``delete_state`` when a subagent-derived agent is destroyed -- without
# clobbering the other middlewares' top-level session state.
SESSION_STATE_KEYS: tuple[str, ...] = (
    _HISTORY_KEY,
    _WARN_COUNT_KEY,
    _INTERNAL_WARNED_KEY,
    _HALTED_KEY,
    _REASONING_HISTORY_KEY,
    _REASONING_WARNED_KEY,
)

# Maximum number of content hashes kept per session before trimming.
_MAX_HISTORY = 30
# Only the tail of a long content string is hashed, to keep the comparison
# cheap and stable even when the rest of the text changes.
_TAIL_CHARS = 500
# Minimum content/reasoning length before repetition detection runs at all,
# preventing false positives on short responses.
_MIN_CONTENT_LENGTH = 20
# Default minimum consecutive identical non-whitespace characters (e.g. 8x
# ``啊``) required to flag a character run as "repetitive".
_CHAR_RUN_MIN = 8
# Sentence/line delimiter set used by the segment-level sub-detector.  Because
# ``\\n`` is included, line-level repetition is covered by the same pass.
_SENTENCE_SPLIT = re.compile(r"[。.!?！？\n]+")
# ``additional_kwargs`` keys that reasoning providers use to carry explicit
# chain-of-thought / reasoning text alongside the visible content.
_REASONING_KEYS = ("reasoning_content", "reasoning", "reasoning_text")
# Inline chain-of-thought wrappers emitted by various reasoning models.
# <think> / <thinking> — DeepSeek-R1 & OpenAI-style; <reasoning> — Qwen
# local / GGUF variants. Order matters for extraction/strip determinism.
_THINK_PATTERNS = [
    re.compile(r"<think>(.*?)</think>", re.DOTALL),
    re.compile(r"<thinking>(.*?)</thinking>", re.DOTALL),
    re.compile(r"<reasoning>(.*?)</reasoning>", re.DOTALL),
]

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
    def _content_hash(content: str) -> str:
        """Hash the tail of the content for cross-call comparison.

        Only the last ``_TAIL_CHARS`` characters are considered so that two
        outputs whose stable prefix differs but whose closing lines match (the
        actual "loop tail") still compare equal.
        """
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
        """Segment-level sub-detector: duplicate ratio of sentences/lines.

        Splits ``content`` on sentence-/line-ending punctuation (and ``\\n``,
        so this doubles as the line detector), then computes
        ``1 - (unique / total)``.  Returns ``True`` if more than
        ``internal_repeat_ratio`` of the segments are duplicates and there are
        at least ``internal_min_lines`` of them (to avoid short-response false
        positives).
        """
        sentences = [s.strip() for s in _SENTENCE_SPLIT.split(content) if s.strip()]
        if len(sentences) < self.internal_min_lines:
            return False
        unique = len(set(sentences))
        ratio = 1.0 - (unique / len(sentences))
        return ratio > self.internal_repeat_ratio

    def _detect_char_run(self, content: str) -> bool:
        """Character-run sub-detector: same char repeated consecutively.

        Uses a regex back-reference ``\\1`` to find ``char_run_min`` or more
        consecutive identical non-whitespace characters (e.g. ``啊啊啊啊…``),
        which is a strong signal of stuttering/death-loop output.
        """
        threshold = self.char_run_min - 1
        pattern = rf"([^\s])\1{{{threshold},}}"
        return bool(re.search(pattern, content))

    def _detect_phrase_repetition(
        self,
        content: str,
        min_repeats: int = 5,
        max_phrase: int = 10,
    ) -> bool:
        """Phrase-periodic sub-detector: short substring repeated back-to-back.

        Looks for any phrase of length ``2..max_phrase`` that repeats
        immediately and contiguously ``min_repeats``+ times with no
        delimiter -- e.g. ``我来帮你我来帮你我来帮你我来帮你``.  Whitespace is
        stripped first so delimiters do not mask the pattern.
        """
        content = re.sub(r"[ \t]+", "", content)
        n = len(content)
        if n < 2 * min_repeats:
            return False

        # A phrase long enough to repeat ``min_repeats`` times cannot exceed
        # ``n // min_repeats`` chars -- this bounds the search space.
        upper = min(max_phrase, n // min_repeats)
        for plen in range(2, upper + 1):
            # Try every feasible start offset so the pattern is not required
            # to begin at index 0 (the string may have a non-looping prefix).
            limit = n - plen * min_repeats
            for start in range(min(plen, limit + 1)):
                pattern = content[start:start + plen]
                if not pattern.strip():
                    continue
                # Greedy contiguous repetition count from this offset.
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
        """Extract explicit reasoning text stored on ``additional_kwargs``.

        Checks the keys in :data:`_REASONING_KEYS` in order and returns the
        first non-empty value, stripped.
        """
        ak = getattr(msg, "additional_kwargs", None)
        if ak and isinstance(ak, dict):
            for key in _REASONING_KEYS:
                reasoning = str(ak.get(key, "") or "").strip()
                if reasoning:
                    return reasoning
        return ""

    @staticmethod
    def _extract_inline_reasoning(content: str) -> str:
        """Extract reasoning text wrapped in inline CoT tags (``<think>`` etc.).

        Concatenates the captured body of every matching tag (in ``_THINK_PATTERNS``
        order) separated by newlines.  Used as a fallback when no explicit
        ``additional_kwargs`` reasoning is available.
        """
        parts = [
            m.group(1).strip()
            for pattern in _THINK_PATTERNS
            for m in pattern.finditer(content)
            if m.group(1).strip()
        ]
        return "\n".join(parts)

    @staticmethod
    def _strip_inline_reasoning(content: str) -> str:
        """Remove inline CoT tags (``<think>`` etc.) from visible content.

        The complement of :func:`_extract_inline_reasoning` -- once reasoning
        has been extracted, the tags are removed from the visible output so the
        repetition detector operates on the cleaned text.  Returns the stripped,
        trimmed remainder.
        """
        for pattern in _THINK_PATTERNS:
            content = pattern.sub("", content)
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
        """Shared cross-call + internal repetition check for any text stream.

        Used both for the visible output and for reasoning text, parametrised
        by the state keys and a ``label`` used in log/message text.

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
        # Rolling history of content hashes; ``consecutive`` is the length of
        # the run of hashes equal to the current one at the tail of the list.
        history: list[str] = state_register_mem.get_state(
            session_id, history_key, []
        )

        consecutive = 0
        for h in reversed(history):
            if h == ch:
                consecutive += 1
            else:
                break

        # Append the current hash and cap the window.
        history.append(ch)
        if len(history) > _MAX_HISTORY:
            history = history[-_MAX_HISTORY:]
        state_register_mem.set_state(session_id, history_key, history)

        # This occurrence itself counts toward the run.
        total_identical = consecutive + 1

        # ---- 1. Hard halt ----------------------------------------------
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

        # ---- 2. Soft warning --------------------------------------------
        if total_identical >= self.warn_after:
            logger.debug(
                "[OutputRepetitionGuard] session={} cross-call "
                "{} repetition warning (total_identical={})",
                session_id, label, total_identical,
            )
            # Keep the model's own text so we only *nudge*, never erase it.
            prefix = (content_prefix + "\n\n") if content_prefix else ""
            return AIMessage(
                content=prefix
                + f" [Output Repetition Guard] Detected {label} "
                f"repetition ({total_identical} times). Please change "
                f"your approach or provide a final answer."
            )

        # ---- 3. Internal repetition (at most once per session/label) ----
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

        # Skip if model is making tool calls -- that's ToolGuardrails' job
        tool_calls = getattr(ai_msg, "tool_calls", None)
        if tool_calls:
            return None

        content = str(ai_msg.content or "").strip()
        reasoning = self._extract_reasoning(ai_msg)

        # Fall back to inline <think>...</think> style reasoning: extract it and
        # strip the tags from the visible content before detection.
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

        # Guard the (cheap-but-nonzero) detection work behind a length
        # threshold to avoid false positives on short responses.
        if len(content) >= _MIN_CONTENT_LENGTH:
            r = self._check_text_repetition(
                session_id, content, content,
                _HISTORY_KEY, _INTERNAL_WARNED_KEY,
                "output",
            )
            if r is not None:
                return r

        # Reasoning streams are checked independently and share the visible
        # content as the ``content_prefix`` so a warning keeps context.
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
        """Reset all per-session repetition state at the start of each turn.

        Clears the output/reasoning history windows and resets the warning and
        halt flags so a fresh turn starts with a clean slate.
        """
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
        """Synchronous lifecycle hook: reset repetition state for this turn.

        Delegates to :func:`_before_agent_impl` and returns ``None``, leaving
        the agent state unchanged.
        """
        logger.debug("{} before_agent hook fired", type(self).__name__)
        self._before_agent_impl(state)
        return None

    @override
    async def abefore_agent(
        self, state: AgentState, runtime: Runtime[ContextT]
    ) -> dict[str, Any] | None:
        """Async lifecycle hook: same as :func:`before_agent` for async runs."""
        logger.debug("{} abefore_agent hook fired", type(self).__name__)
        self._before_agent_impl(state)
        return None

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

    already_warned: bool = state_register_mem.get_state(
        session_id, _INTERNAL_WARNED_KEY, False
    )
    if already_warned:
        return None

    state_register_mem.set_state(session_id, _INTERNAL_WARNED_KEY, True)
    logger.debug(
        "[OutputRepetitionGuard] session={} stream internal repetition "
        "detected -- cutting output",
        session_id,
    )
    return _STREAM_WARNING