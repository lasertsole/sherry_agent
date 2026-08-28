"""RepetitionGuardWrapper — wraps a CompiledStateGraph agent with comprehensive
output repetition detection at the Runnable / stream level.

This wrapper implements **ALL** the interception functionality of the
``OutputRepetitionGuard`` middleware, operating at the stream level instead of
the middleware level:

1. **Stream-level internal repetition detection** (sentence, char-run, phrase)
   — runs on accumulated visible text as chunks arrive, cutting the repetitive
   tail *before* it reaches the client.

2. **Cross-call identical output detection** — runs at model-call boundaries
   (detected by stream node transitions), comparing the accumulated text of
   each model call against a rolling hash history.

3. **Reasoning text repetition detection** — tracks ``reasoning_content``
   independently from visible output, with its own history and warn flags.

4. **HALT escalation** — when cross-call repetition exceeds the threshold,
   yields a halt message and cancels the underlying stream.

5. **WARN escalation** — yields a warning message (for cross-call) or cuts the
   current call's remaining text (for internal repetition).

6. **Per-turn state reset** — clears all repetition state at the start of each
   ``astream`` / ``ainvoke`` call, mirroring the middleware's ``before_agent``.

7. **Non-streaming (``ainvoke``) post-hoc detection** — runs the full detection
   suite on the final ``AIMessage`` of the result.

8. **HALT short-circuit** — when ``_HALTED_KEY`` is already set (e.g. by the
   middleware backstop), subsequent model calls yield a halt message instead of
   forwarding repetitive text.

When using this wrapper as the **sole** guardian, remove
``OutputRepetitionGuard`` from the agent's middleware list to avoid
double-counting in the cross-call history.  If both are active, the
``_INTERNAL_WARNED_KEY`` dedupe gate prevents double-warning, but the
cross-call hash history may accumulate duplicate entries.

Integration (in ``agent/core.py``)::

    from .repetition_guard_wrapper import RepetitionGuardWrapper

    _agent = create_agent(...)
    _agent = RepetitionGuardWrapper(_agent)   # wrap here
    return _agent

And in ``server/service/messages.py``, remove the manual
``check_stream_repetition`` calls — the wrapper handles stream-level
interception transparently.
"""

from __future__ import annotations

from typing import Any, AsyncGenerator

from loguru import logger
from langchain_core.messages import AIMessage, AIMessageChunk
from langgraph.graph.state import CompiledStateGraph

from runtime import state_register_mem
from agent.middlewares.output_repetition_guard import (
    OutputRepetitionGuard,
    SESSION_STATE_KEYS,
    _HISTORY_KEY,
    _WARN_COUNT_KEY,
    _INTERNAL_WARNED_KEY,
    _HALTED_KEY,
    _REASONING_HISTORY_KEY,
    _REASONING_WARNED_KEY,
    _MAX_HISTORY,
    _TAIL_CHARS,
    _MIN_CONTENT_LENGTH,
    _CHAR_RUN_MIN,
    _STREAM_WARNING,
    _REASONING_KEYS,
)

# Reasoning keys used to extract reasoning text from ``additional_kwargs``.
# Re-exported from the middleware module so the wrapper and middleware stay
# in sync.
__all__ = [
    "RepetitionGuardWrapper",
    "SESSION_STATE_KEYS",
]


class RepetitionGuardWrapper:
    """Wraps a ``CompiledStateGraph`` with stream-level repetition interception.

    Parameters
    ----------
    inner : CompiledStateGraph
        The compiled agent graph to wrap.
    max_identical_outputs : int
        Maximum consecutive identical model outputs before a hard stop.
        Default **3**.
    warn_after : int
        Consecutive identical outputs before a warning is appended.
        Default **2**.
    internal_repeat_ratio : float
        Duplicate ratio above which a single output is internally repetitive.
        Default **0.6**.
    internal_min_lines : int
        Minimum segments before sentence-level detection fires.
        Default **6**.
    char_run_min : int
        Minimum consecutive identical non-whitespace characters.
        Default **8**.
    """

    def __init__(
        self,
        inner: CompiledStateGraph,
        max_identical_outputs: int = 3,
        warn_after: int = 2,
        internal_repeat_ratio: float = 0.6,
        internal_min_lines: int = 6,
        char_run_min: int = _CHAR_RUN_MIN,
        phantom_stream_guard: bool = False,
    ):
        self._inner = inner
        self._guard = OutputRepetitionGuard(
            max_identical_outputs=max_identical_outputs,
            warn_after=warn_after,
            internal_repeat_ratio=internal_repeat_ratio,
            internal_min_lines=internal_min_lines,
            char_run_min=char_run_min,
        )
        # Opt-in phantom-stream guard (see astream for details). Enable
        # ONLY for graphs whose middleware lifecycle guarantees
        # "updates" chunks before any model text on fresh dict-input
        # runs (true for the production agent built in agent/core.py).
        self._phantom_stream_guard = phantom_stream_guard

    # ------------------------------------------------------------------
    # Session helpers
    # ------------------------------------------------------------------
    @staticmethod
    def _extract_session_id(input_: Any, config: Any) -> str:
        """Extract ``session_id`` from the input dict, ``Command.resume``,
        or ``config['configurable']``.

        Raises ``RuntimeError`` if it cannot be found.
        """
        # 1. Plain dict input (normal astream / ainvoke path)
        if isinstance(input_, dict):
            sid = input_.get("session_id", "")
            if sid.strip():
                return sid

        # 2. Command(resume=...) — HITL resume path
        try:
            from langgraph.types import Command

            if isinstance(input_, Command):
                resume = getattr(input_, "resume", None)
                if isinstance(resume, dict):
                    sid = resume.get("session_id", "")
                    if sid.strip():
                        return sid
        except Exception:
            pass

        # 3. config configurable fallback
        try:
            cfg = config or {}
            if isinstance(cfg, dict):
                conf = cfg.get("configurable", {})
                if isinstance(conf, dict):
                    sid = conf.get("session_id", "")
                    if sid.strip():
                        return sid
        except Exception:
            pass

        raise RuntimeError(
            "RepetitionGuardWrapper: session_id is required but not found "
            "in input, Command.resume, or config.configurable"
        )

    # ------------------------------------------------------------------
    # Stream-mode detection
    # ------------------------------------------------------------------
    @staticmethod
    def _can_intercept(stream_mode: Any) -> bool:
        """Return ``True`` when ``stream_mode`` includes ``"messages"``."""
        if stream_mode is None:
            return False
        if isinstance(stream_mode, str):
            return stream_mode == "messages"
        if isinstance(stream_mode, (list, tuple)):
            return "messages" in stream_mode
        return False

    # ------------------------------------------------------------------
    # Chunk builders
    # ------------------------------------------------------------------
    @staticmethod
    def _text_chunk(
        content: str,
        metadata: dict[str, Any] | None = None,
    ) -> tuple[str, tuple[AIMessageChunk, dict[str, Any]]]:
        """Build a ``("messages", (AIMessageChunk, metadata))`` chunk."""
        return (
            "messages",
            (
                AIMessageChunk(content=content),
                metadata or {"langgraph_node": "model"},
            ),
        )

    @staticmethod
    def _halt_message() -> str:
        """The HALT message text yielded when repetition forces a stop."""
        return (
            "[Output Repetition Guard] Output repetition was detected. "
            "I must stop here. Please summarize what has been accomplished "
            "and what remains to be done."
        )

    @staticmethod
    def _halted_short_circuit_message() -> str:
        """Message yielded when ``_HALTED_KEY`` is already set."""
        return (
            "[Output Repetition Guard] Output repetition was detected "
            "earlier this turn. I must stop here."
        )

    # ------------------------------------------------------------------
    # Boundary detection (cross-call + internal)
    # ------------------------------------------------------------------
    def _on_model_call_end(
        self,
        session_id: str,
        call_text: str,
        call_reasoning: str,
    ) -> tuple[str | None, bool]:
        """Run cross-call + internal detection at a model-call boundary.

        Returns ``(warning_text, is_halt)``.  When ``warning_text`` is
        ``None``, no escalation applies.  When ``is_halt`` is ``True``, the
        caller should cancel the stream.
        """
        # ---- visible output ----
        if len(call_text) >= _MIN_CONTENT_LENGTH:
            r = self._guard._check_text_repetition(
                session_id,
                call_text,
                call_text,
                _HISTORY_KEY,
                _INTERNAL_WARNED_KEY,
                "output",
            )
            if r is not None:
                is_halt = state_register_mem.get_state(
                    session_id, _HALTED_KEY, False
                )
                return (r.content, is_halt)

        # ---- reasoning (independent history) ----
        if len(call_reasoning) >= _MIN_CONTENT_LENGTH:
            r = self._guard._check_text_repetition(
                session_id,
                call_reasoning,
                call_text,
                _REASONING_HISTORY_KEY,
                _REASONING_WARNED_KEY,
                "reasoning",
            )
            if r is not None:
                is_halt = state_register_mem.get_state(
                    session_id, _HALTED_KEY, False
                )
                return (r.content, is_halt)

        return (None, False)

    # ------------------------------------------------------------------
    # Non-streaming post-hoc check
    # ------------------------------------------------------------------
    def _post_hoc_check(
        self,
        session_id: str,
        ai_msg: AIMessage,
    ) -> AIMessage | None:
        """Post-hoc detection on a complete ``AIMessage`` (for ``ainvoke``).

        Mirrors ``OutputRepetitionGuard._wrap_model_call_post`` but operates
        on a standalone message instead of a middleware ``ModelRequest``.
        """
        # Skip if model is making tool calls
        tool_calls = getattr(ai_msg, "tool_calls", None)
        if tool_calls:
            return None

        content = str(ai_msg.content or "").strip()
        reasoning = OutputRepetitionGuard._extract_reasoning(ai_msg)

        # Fall back to inline <think>…</think> style reasoning
        if not reasoning:
            reasoning = OutputRepetitionGuard._extract_inline_reasoning(content)
            if reasoning:
                content = OutputRepetitionGuard._strip_inline_reasoning(content)

        # Halted short-circuit
        if state_register_mem.get_state(session_id, _HALTED_KEY, False):
            return AIMessage(content=self._halted_short_circuit_message())

        # Visible output
        if len(content) >= _MIN_CONTENT_LENGTH:
            r = self._guard._check_text_repetition(
                session_id,
                content,
                content,
                _HISTORY_KEY,
                _INTERNAL_WARNED_KEY,
                "output",
            )
            if r is not None:
                return r

        # Reasoning
        if len(reasoning) >= _MIN_CONTENT_LENGTH:
            r = self._guard._check_text_repetition(
                session_id,
                reasoning,
                content,
                _REASONING_HISTORY_KEY,
                _REASONING_WARNED_KEY,
                "reasoning",
            )
            if r is not None:
                return r

        return None

    # ------------------------------------------------------------------
    # Streaming interception (astream)
    # ------------------------------------------------------------------
    async def astream(self, *args, **kwargs) -> AsyncGenerator[tuple, None]:
        """Intercepted streaming with comprehensive repetition detection.

        Accepts the same arguments as ``CompiledStateGraph.astream`` and
        yields the same ``(mode, data)`` chunk format.  When repetition is
        detected, a warning chunk is yielded and subsequent text from the
        current (or all) model call(s) is suppressed.
        """
        input_ = args[0] if args else kwargs.get("input")
        config = args[1] if len(args) > 1 else kwargs.get("config")
        stream_mode = kwargs.get("stream_mode")

        session_id = self._extract_session_id(input_, config)

        # [RGW-DIAG] temporary instrumentation
        try:
            logger.debug(
                "[RGW-DIAG] astream ENTRY session={} stream_mode={} input={!r}",
                session_id,
                stream_mode,
                str(input_)[:300],
            )
        except Exception:
            pass

        # Per-turn state reset (mirrors middleware before_agent)
        self._guard._before_agent_impl({"session_id": session_id})

        # If stream_mode doesn't include "messages", pass through without
        # interception — we can only detect repetition on message chunks.
        if not self._can_intercept(stream_mode):
            async for chunk in self._inner.astream(*args, **kwargs):
                yield chunk
            return

        # State machine for tracking model-call boundaries
        in_model_call = False
        call_text = ""
        call_reasoning = ""
        call_cut = False  # whether current call's visible text was cut

        # [phantom-stream guard] state: a fresh dict-input run ALWAYS emits
        # middleware before_agent "updates" tuples before any model text
        # (verified via [RGW-DIAG] healthy-turn captures — first chunk is
        # {'MultimodalProcessor.before_agent': None}). "Model"-tagged text
        # arriving before ANY update cannot be live graph output; when it
        # tripped the internal-repetition cut, call_cut then suppressed the
        # REAL reply behind it (user saw only the 145-char warning).
        saw_updates = False       # any "updates" chunk seen (graph started)
        phantom_dropped = 0       # pre-updates "model" text chunks dropped
        # Command(resume) streams legitimately start with messages (the
        # interrupted node re-executes without re-running before_agent),
        # so the guard only applies to fresh dict-input runs. The guard
        # itself is opt-in (constructor flag) — enabled in production.
        phantom_guard_active = self._phantom_stream_guard and isinstance(
            input_, dict
        )

        generator = self._inner.astream(*args, **kwargs)

        try:
            async for chunk in generator:
                # Guard against unexpected chunk shapes
                if not isinstance(chunk, (tuple, list)) or len(chunk) < 2:
                    yield chunk
                    continue

                mode = chunk[0]
                data = chunk[1]

                # [RGW-DIAG] temporary instrumentation
                try:
                    if isinstance(data, (tuple, list)) and len(data) >= 2:
                        _d0, _d1 = data[0], data[1]
                        _meta = _d1 if isinstance(_d1, dict) else {}
                        logger.debug(
                            "[RGW-DIAG] chunk mode={} type={} node={} step={} "
                            "content={!r} tc={} akw={} in_call={} ct_len={} cut={}",
                            mode,
                            type(_d0).__name__,
                            _meta.get("langgraph_node"),
                            _meta.get("langgraph_step"),
                            str(getattr(_d0, "content", None))[:120],
                            getattr(_d0, "tool_calls", None)
                            or getattr(_d0, "tool_call_chunks", None),
                            list(getattr(_d0, "additional_kwargs", {}) or {}),
                            in_model_call,
                            len(call_text),
                            call_cut,
                        )
                    else:
                        logger.debug(
                            "[RGW-DIAG] chunk RAW mode={} data={!r}",
                            mode,
                            str(data)[:200],
                        )
                except Exception:
                    pass

                # ====================================================
                # "updates" mode — model call boundary
                # ====================================================
                if mode == "updates":
                    # Graph has started — the phantom-stream guard disarms.
                    saw_updates = True
                    if in_model_call:
                        warning, is_halt = self._on_model_call_end(
                            session_id, call_text, call_reasoning
                        )
                        # Reset state BEFORE yielding/breaking so the
                        # end-of-stream block doesn't re-process this call.
                        in_model_call = False
                        call_text = ""
                        call_reasoning = ""
                        call_cut = False
                        if warning is not None:
                            yield self._text_chunk(warning)
                            if is_halt:
                                logger.warning(
                                    "[RepetitionGuardWrapper] session={} "
                                    "cross-call HALT — cancelling stream",
                                    session_id,
                                )
                                break

                    yield chunk
                    continue

                # ====================================================
                # non-"messages" mode — pass through
                # ====================================================
                if mode != "messages":
                    yield chunk
                    continue

                # data is (message_chunk, metadata_dict)
                if not isinstance(data, (tuple, list)) or len(data) < 2:
                    yield chunk
                    continue

                msg_chunk: Any = data[0]
                metadata: dict = data[1] if isinstance(data[1], dict) else {}
                node = metadata.get("langgraph_node")

                # --------------------------------------------------
                # Non-model node -> boundary if we were in a call
                # --------------------------------------------------
                if node != "model":
                    if in_model_call:
                        warning, is_halt = self._on_model_call_end(
                            session_id, call_text, call_reasoning
                        )
                        # Reset state BEFORE yielding/breaking so the
                        # end-of-stream block doesn't re-process this call.
                        in_model_call = False
                        call_text = ""
                        call_reasoning = ""
                        call_cut = False
                        if warning is not None:
                            yield self._text_chunk(warning)
                            if is_halt:
                                logger.warning(
                                    "[RepetitionGuardWrapper] session={} "
                                    "cross-call HALT — cancelling stream",
                                    session_id,
                                )
                                break
                    yield chunk
                    continue

                # --------------------------------------------------
                # Model node — we're inside a model call
                # --------------------------------------------------
                # [phantom-stream guard] On a fresh dict-input run the
                # middleware-equipped graph ALWAYS emits before_agent
                # "updates" tuples before any model text (verified via
                # [RGW-DIAG] healthy-turn captures — first chunk is
                # {'MultimodalProcessor.before_agent': None}).
                # "Model"-tagged text arriving before ANY update cannot
                # be live graph output; historically it tripped the
                # internal-repetition cut and call_cut then suppressed
                # the REAL reply behind it (user saw only the 145-char
                # warning). Drop the phantom loudly instead of cutting.
                if (
                    phantom_guard_active
                    and not saw_updates
                    and str(getattr(msg_chunk, "content", "") or "")
                ):
                    phantom_dropped += 1
                    if phantom_dropped == 1:
                        logger.critical(
                            "[RepetitionGuardWrapper] PHANTOM model stream "
                            "before first graph update — dropping. "
                            "session={} node={} metadata={!r} content={!r}",
                            session_id,
                            node,
                            metadata,
                            str(getattr(msg_chunk, "content", ""))[:200],
                        )
                    continue
                in_model_call = True

                # HALT short-circuit: if the halt flag is already set
                # (e.g. by the middleware backstop), yield halt messages
                # instead of forwarding repetitive text.
                if state_register_mem.get_state(session_id, _HALTED_KEY, False):
                    yield self._text_chunk(
                        self._halted_short_circuit_message(), metadata
                    )
                    continue

                if not isinstance(msg_chunk, AIMessageChunk):
                    yield chunk
                    continue

                # Skip chunks that carry tool calls (text-output guard only)
                has_tool_calls = bool(
                    getattr(msg_chunk, "tool_calls", None)
                    or getattr(msg_chunk, "tool_call_chunks", None)
                )

                # Extract text content and reasoning
                content = str(msg_chunk.content or "")
                reasoning = ""
                ak = getattr(msg_chunk, "additional_kwargs", None)
                if ak and isinstance(ak, dict):
                    for rk in _REASONING_KEYS:
                        rv = str(ak.get(rk, "") or "")
                        if rv:
                            reasoning = rv
                            break

                # ---- accumulate + stream-level internal detection ----
                if content and not call_cut and not has_tool_calls:
                    call_text += content
                    if len(call_text) >= _MIN_CONTENT_LENGTH:
                        try:
                            if self._guard._detect_internal_repetition(call_text):
                                already = state_register_mem.get_state(
                                    session_id, _INTERNAL_WARNED_KEY, False
                                )
                                if not already:
                                    state_register_mem.set_state(
                                        session_id, _INTERNAL_WARNED_KEY, True
                                    )
                                    logger.debug(
                                        "[RepetitionGuardWrapper] session={} "
                                        "stream internal repetition — cutting; "
                                        "call_text={!r}",
                                        session_id,
                                        call_text[:200],
                                    )
                                    yield self._text_chunk(
                                        _STREAM_WARNING, metadata
                                    )
                                    call_cut = True
                        except Exception:
                            logger.exception(
                                "[RepetitionGuardWrapper] internal detection "
                                "error (non-fatal)"
                            )

                # ---- accumulate reasoning (checked at boundary) ----
                if reasoning:
                    call_reasoning += reasoning

                # ---- forward chunk ----
                # When text is cut, skip subsequent text-bearing chunks from
                # the current model call.  Reasoning-only chunks (empty
                # content) are still forwarded so the thinking stream stays
                # intact for the client.
                if call_cut and content and not has_tool_calls:
                    continue  # suppress repetitive text
                yield chunk

            # ---- end of stream: process the last model call ----
            if in_model_call:
                warning, is_halt = self._on_model_call_end(
                    session_id, call_text, call_reasoning
                )
                if warning is not None:
                    yield self._text_chunk(warning)

        finally:
            try:
                logger.debug("[RGW-DIAG] astream EXIT session={}", session_id)
            except Exception:
                pass
            if phantom_dropped > 0:
                try:
                    logger.critical(
                        "[RepetitionGuardWrapper] PHANTOM stream total: "
                        "dropped {} pre-update model chunk(s) session={}",
                        phantom_dropped,
                        session_id,
                    )
                except Exception:
                    pass
            if generator is not None:
                try:
                    await generator.aclose()
                except Exception:
                    pass

    # ------------------------------------------------------------------
    # Non-streaming (ainvoke)
    # ------------------------------------------------------------------
    async def ainvoke(self, *args, **kwargs) -> Any:
        """Intercepted non-streaming with post-hoc repetition detection.

        Resets per-turn state, delegates to the inner agent, then inspects
        the final ``AIMessage`` of the result.  If repetition is detected,
        the last message is replaced with the guard's warning/halt message.
        """
        input_ = args[0] if args else kwargs.get("input")
        config = args[1] if len(args) > 1 else kwargs.get("config")

        session_id = self._extract_session_id(input_, config)

        # Per-turn state reset
        self._guard._before_agent_impl({"session_id": session_id})

        result = await self._inner.ainvoke(*args, **kwargs)

        # Post-hoc detection on the last AIMessage
        if isinstance(result, dict) and "messages" in result:
            messages = result["messages"]
            if messages:
                last_msg = messages[-1]
                if isinstance(last_msg, AIMessage):
                    try:
                        replacement = self._post_hoc_check(session_id, last_msg)
                        if replacement is not None:
                            result["messages"][-1] = replacement
                    except Exception:
                        logger.exception(
                            "[RepetitionGuardWrapper] post-hoc detection "
                            "error (non-fatal)"
                        )

        return result

    # ------------------------------------------------------------------
    # Transparent delegation
    # ------------------------------------------------------------------
    def __getattr__(self, name: str) -> Any:
        """Delegate any unknown attribute access to the inner agent.

        This ensures that methods like ``aget_state``, ``aupdate_state``,
        ``aget_state_history``, etc. are transparently forwarded.
        """
        return getattr(self._inner, name)

    # ------------------------------------------------------------------
    # Convenience accessors
    # ------------------------------------------------------------------
    @property
    def inner(self) -> CompiledStateGraph:
        """The wrapped inner agent graph."""
        return self._inner
