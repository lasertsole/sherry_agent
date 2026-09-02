"""RepetitionGuardWrapper (slim) — wraps a CompiledStateGraph agent with
**stream-only** internal repetition detection.

This is the slimmed-down version: the wrapper handles ONLY what the
middleware cannot — real-time stream-level internal repetition cutting.
All cross-call detection, per-turn state reset, post-hoc (ainvoke)
detection, reasoning repetition, and HALT escalation are handled by
``OutputRepetitionGuard`` middleware on the inner agent.

Responsibilities retained:

1. **Stream-level internal repetition detection** (sentence, char-run,
   phrase) — runs on accumulated visible text as chunks arrive, cutting
   the repetitive tail *before* it reaches the client.

2. **HALT short-circuit** — when ``_HALTED_KEY`` is set by the
   middleware (cross-call HALT), subsequent model calls yield a halt
   message instead of forwarding repetitive text.

3. **Phantom-stream guard** — drops pre-update model text on fresh
   dict-input runs that cannot be real graph output.

Everything else is delegated to the middleware:
- Cross-call identical-output detection  → ``wrap_model_call``
- Per-turn state reset                   → ``before_agent``
- Non-streaming (ainvoke) post-hoc       → ``wrap_model_call``
- Reasoning text repetition              → ``_wrap_model_call_post``
- HALT escalation (setting _HALTED_KEY)  → ``_check_text_repetition``

Integration (in ``agent/core.py``)::

    from .stream_repetition_guard_wrapper import RepetitionGuardWrapper
    from .middlewares.output_repetition_guard import OutputRepetitionGuard

    _agent = create_agent(
        ...,
        middleware=[
            ...,
            OutputRepetitionGuard(),  # cross-call + post-hoc + state reset
        ],
    )
    _agent = RepetitionGuardWrapper(_agent, phantom_stream_guard=True)
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, AsyncGenerator

from loguru import logger
from langchain_core.messages import AIMessageChunk
from langgraph.graph.state import CompiledStateGraph

from runtime import state_register_mem
from agent.middlewares.output_repetition_guard import (
    OutputRepetitionGuard,
    SESSION_STATE_KEYS,
    _INTERNAL_WARNED_KEY,
    _HALTED_KEY,
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
        internal_repeat_ratio: float = 0.6,
        internal_min_lines: int = 6,
        char_run_min: int = _CHAR_RUN_MIN,
        phantom_stream_guard: bool = False,
    ):
        self._inner = inner
        self._guard = OutputRepetitionGuard(
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
    def _halted_short_circuit_message() -> str:
        """Message yielded when ``_HALTED_KEY`` is already set."""
        return (
            "[Output Repetition Guard] Output repetition was detected "
            "earlier this turn. I must stop here."
        )

    # ------------------------------------------------------------------
    # Streaming interception (astream)
    # ------------------------------------------------------------------
    async def astream(self, *args, **kwargs) -> AsyncGenerator[tuple, None]:
        """Intercepted streaming with stream-level internal repetition cutting.

        Accepts the same arguments as ``CompiledStateGraph.astream`` and
        yields the same ``(mode, data)`` chunk format.  When internal
        repetition is detected, a warning chunk is yielded and subsequent
        text from the current model call is suppressed.  Cross-call
        detection, reasoning detection, HALT escalation and per-turn
        state reset are all handled by the ``OutputRepetitionGuard``
        middleware on the inner agent.
        """
        input_ = args[0] if args else kwargs.get("input")
        config = args[1] if len(args) > 1 else kwargs.get("config")
        stream_mode = kwargs.get("stream_mode")

        session_id = self._extract_session_id(input_, config)

        # If stream_mode doesn't include "messages", pass through without
        # interception — we can only detect repetition on message chunks.
        if not self._can_intercept(stream_mode):
            async for chunk in self._inner.astream(*args, **kwargs):
                yield chunk
            return

        # State machine for tracking the current model call's accumulated
        # text (for internal repetition detection only).
        call_text = ""
        call_cut = False  # whether current call's visible text was cut

        # [phantom-stream guard] state: a fresh dict-input run ALWAYS emits
        # middleware before_agent "updates" tuples before any model text
        # (verified via healthy-turn captures — first chunk is
        # {'MultimodalProcessor.before_agent': None}). "Model"-tagged text
        # arriving before ANY update cannot be live graph output; when it
        # tripped the internal-repetition cut, call_cut then suppressed the
        # REAL reply behind it (user saw only the 145-char warning).
        saw_updates = False  # any "updates" chunk seen (graph started)
        phantom_dropped = 0  # pre-updates "model" text chunks dropped
        # Command(resume) streams legitimately start with messages (the
        # interrupted node re-executes without re-running before_agent),
        # so the guard only applies to fresh dict-input runs. The guard
        # itself is opt-in (constructor flag) — enabled in production.
        phantom_guard_active = self._phantom_stream_guard and isinstance(input_, dict)

        generator = self._inner.astream(*args, **kwargs)

        try:
            async for chunk in generator:
                # Guard against unexpected chunk shapes
                if not isinstance(chunk, (tuple, list)) or len(chunk) < 2:
                    yield chunk
                    continue

                mode = chunk[0]
                data = chunk[1]

                # ====================================================
                # "updates" mode — reset per-call tracking
                # ====================================================
                if mode == "updates":
                    saw_updates = True
                    # Reset per-call tracking (cross-call detection is
                    # handled by the middleware's wrap_model_call).
                    call_text = ""
                    call_cut = False
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
                # Non-model node -> reset per-call tracking
                # --------------------------------------------------
                if node != "model":
                    call_text = ""
                    call_cut = False
                    yield chunk
                    continue

                # --------------------------------------------------
                # Model node — internal repetition detection
                # --------------------------------------------------
                # [phantom-stream guard] On a fresh dict-input run the
                # middleware-equipped graph ALWAYS emits before_agent
                # "updates" tuples before any model text (verified via
                # healthy-turn captures — first chunk is
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

                # HALT short-circuit: if the halt flag is already set
                # (e.g. by the middleware backstop), yield halt messages
                # instead of forwarding repetitive text.
                if state_register_mem.get_state(session_id, _HALTED_KEY, False):
                    yield self._text_chunk(self._halted_short_circuit_message(), metadata)
                    continue

                if not isinstance(msg_chunk, AIMessageChunk):
                    yield chunk
                    continue

                # Skip chunks that carry tool calls (text-output guard only)
                has_tool_calls = bool(
                    getattr(msg_chunk, "tool_calls", None)
                    or getattr(msg_chunk, "tool_call_chunks", None)
                )

                content = str(msg_chunk.content or "")

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
                                    yield self._text_chunk(_STREAM_WARNING, metadata)
                                    call_cut = True
                        except Exception:
                            logger.exception(
                                "[RepetitionGuardWrapper] internal detection error (non-fatal)"
                            )

                # ---- forward chunk ----
                # When text is cut, skip subsequent text-bearing chunks from
                # the current model call.  Reasoning-only chunks (empty
                # content) are still forwarded so the thinking stream stays
                # intact for the client.
                if call_cut and content and not has_tool_calls:
                    continue  # suppress repetitive text
                yield chunk

        finally:
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
        """Delegate to the inner agent.

        The ``OutputRepetitionGuard`` middleware on the inner agent
        handles all post-hoc detection (cross-call, internal, reasoning)
        via ``wrap_model_call``.  The wrapper does not need to do anything
        here.
        """
        return await self._inner.ainvoke(*args, **kwargs)

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
