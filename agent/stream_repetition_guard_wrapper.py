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

import abc
from typing import Any
from collections.abc import AsyncGenerator

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
)

# Reasoning keys used to extract reasoning text from ``additional_kwargs``.
# Re-exported from the middleware module so the wrapper and middleware stay
# in sync.
__all__ = [
    "RepetitionGuardWrapper",
    "SESSION_STATE_KEYS",
]


class StreamGuardState(abc.ABC):
    """One phase of the wrapper's stream-level guard state machine.

    States are stateless strategy objects; the per-stream mutable data
    (accumulated call text, cut flag, phantom counter) lives on the
    :class:`StreamGuardMachine` that dispatches chunks to the active state.
    """

    def __init__(self, machine: StreamGuardMachine):
        self._m = machine

    def on_updates(self) -> StreamGuardState:
        """A graph "updates" chunk arrived: reset per-call tracking."""
        self._m.reset_call_tracking()
        return self._m.updates_seen_state

    def on_non_model_node(self) -> StreamGuardState:
        """A non-model node chunk arrived: reset per-call tracking.

        ``saw_updates`` is unchanged — a fresh (dict-input) run stays
        fresh until the first real "updates" tuple.
        """
        self._m.reset_call_tracking()
        return self._m.updates_seen_state

    async def on_model_chunk(
        self,
        chunk: Any,
        msg_chunk: Any,
        metadata: dict[str, Any],
    ) -> AsyncGenerator[tuple]:
        """Template for "model"-node message chunks.

        The HALT short-circuit and the non-``AIMessageChunk`` pass-through
        run in every state; the accumulating/suppressing behaviour is
        state-specific.
        """
        # HALT short-circuit: if the halt flag is already set (e.g. by the
        # middleware backstop), yield halt messages instead of forwarding
        # repetitive text.
        if state_register_mem.get_state(self._m.session_id, _HALTED_KEY, False):
            yield StreamGuardMachine.text_chunk(
                StreamGuardMachine.halted_short_circuit_message(), metadata
            )
            return

        if not isinstance(msg_chunk, AIMessageChunk):
            yield chunk
            return

        has_tool_calls = bool(
            getattr(msg_chunk, "tool_calls", None) or getattr(msg_chunk, "tool_call_chunks", None)
        )
        content = str(msg_chunk.content or "")

        async for out in self._handle_model_text(
            chunk, msg_chunk, metadata, content, has_tool_calls
        ):
            yield out

    @abc.abstractmethod
    def _handle_model_text(
        self,
        chunk: Any,
        msg_chunk: Any,
        metadata: dict[str, Any],
        content: str,
        has_tool_calls: bool,
    ) -> AsyncGenerator[tuple]: ...


class FreshState(StreamGuardState):
    """Pre-first-update phase: the phantom-stream guard window.

    On a fresh dict-input run the middleware-equipped graph ALWAYS emits
    before_agent "updates" tuples before any model text (verified via
    healthy-turn captures — first chunk is
    ``{'MultimodalProcessor.before_agent': None}``). "Model"-tagged text
    arriving before ANY update cannot be live graph output; historically it
    tripped the internal-repetition cut and call_cut then suppressed the
    REAL reply behind it (user saw only the 145-char warning). Drop the
    phantom loudly instead of cutting.
    """

    async def on_model_chunk(
        self,
        chunk: Any,
        msg_chunk: Any,
        metadata: dict[str, Any],
    ) -> AsyncGenerator[tuple]:
        if self._m.phantom_guard_active and str(getattr(msg_chunk, "content", "") or ""):
            self._m.phantom_dropped += 1
            if self._m.phantom_dropped == 1:
                logger.critical(
                    "[RepetitionGuardWrapper] PHANTOM model stream "
                    "before first graph update — dropping. "
                    "session={} node={} metadata={!r} content={!r}",
                    self._m.session_id,
                    metadata.get("langgraph_node"),
                    metadata,
                    str(getattr(msg_chunk, "content", ""))[:200],
                )
            return
        async for out in super().on_model_chunk(chunk, msg_chunk, metadata):
            yield out

    def on_non_model_node(self) -> StreamGuardState:
        # Non-model nodes don't mark the graph as started: stay fresh.
        self._m.reset_call_tracking()
        return self

    async def _handle_model_text(
        self,
        chunk: Any,
        msg_chunk: Any,
        metadata: dict[str, Any],
        content: str,
        has_tool_calls: bool,
    ) -> AsyncGenerator[tuple]:
        async for out in self._m.updates_seen_state._handle_model_text(
            chunk, msg_chunk, metadata, content, has_tool_calls
        ):
            yield out


class UpdatesSeenState(StreamGuardState):
    """Graph started; per-call text tracking is empty/running."""

    async def _handle_model_text(
        self,
        chunk: Any,
        msg_chunk: Any,
        metadata: dict[str, Any],
        content: str,
        has_tool_calls: bool,
    ) -> AsyncGenerator[tuple]:
        m = self._m
        # ---- accumulate + stream-level internal detection ----
        if content and not m.call_cut and not has_tool_calls:
            m.call_text += content
            if len(m.call_text) >= _MIN_CONTENT_LENGTH:
                try:
                    if m.guard._detect_internal_repetition(m.call_text):
                        already = state_register_mem.get_state(
                            m.session_id, _INTERNAL_WARNED_KEY, False
                        )
                        if not already:
                            state_register_mem.set_state(m.session_id, _INTERNAL_WARNED_KEY, True)
                            logger.debug(
                                "[RepetitionGuardWrapper] session={} "
                                "stream internal repetition — cutting; "
                                "call_text={!r}",
                                m.session_id,
                                m.call_text[:200],
                            )
                            yield StreamGuardMachine.text_chunk(_STREAM_WARNING, metadata)
                            m.call_cut = True
                            m.transition(m.cut_state)
                            return  # suppress the triggering chunk too
                except Exception:
                    logger.exception(
                        "[RepetitionGuardWrapper] internal detection error (non-fatal)"
                    )
            m.transition(m.model_text_state)

        # ---- forward chunk ----
        # When text is cut, skip subsequent text-bearing chunks from
        # the current model call.  Reasoning-only chunks (empty
        # content) are still forwarded so the thinking stream stays
        # intact for the client.
        if m.call_cut and content and not has_tool_calls:
            return  # suppress repetitive text
        yield chunk


class ModelTextState(UpdatesSeenState):
    """Per-call text is accumulating (``call_text`` non-empty)."""


class CutState(StreamGuardState):
    """This call's visible text was cut; suppress further text chunks."""

    async def _handle_model_text(
        self,
        chunk: Any,
        msg_chunk: Any,
        metadata: dict[str, Any],
        content: str,
        has_tool_calls: bool,
    ) -> AsyncGenerator[tuple]:
        if content and not has_tool_calls:
            return  # suppress repetitive text
        yield chunk


class StreamGuardMachine:
    """Per-stream state machine behind ``RepetitionGuardWrapper.astream``.

    Owns the mutable per-stream variables (``call_text`` / ``call_cut`` /
    ``phantom_dropped``) and dispatches each inner chunk through the
    active :class:`StreamGuardState`.
    """

    def __init__(self, session_id: str, guard: OutputRepetitionGuard, phantom_guard_active: bool):
        self.session_id = session_id
        self.guard = guard
        # Command(resume) streams legitimately start with messages (the
        # interrupted node re-executes without re-running before_agent),
        # so the guard only applies to fresh dict-input runs. The guard
        # itself is opt-in (constructor flag) — enabled in production.
        self.phantom_guard_active = phantom_guard_active
        self.call_text = ""
        self.call_cut = False
        self.phantom_dropped = 0

        self.updates_seen_state = UpdatesSeenState(self)
        self.model_text_state = ModelTextState(self)
        self.cut_state = CutState(self)
        self._state: StreamGuardState = FreshState(self)

    def transition(self, state: StreamGuardState) -> None:
        self._state = state

    def reset_call_tracking(self) -> None:
        """Reset per-call tracking (cross-call detection is handled by the
        middleware's wrap_model_call)."""
        self.call_text = ""
        self.call_cut = False

    async def on_chunk(self, chunk: Any) -> AsyncGenerator[tuple]:
        """Dispatch one inner chunk through the current state."""
        # Guard against unexpected chunk shapes
        if not isinstance(chunk, (tuple, list)) or len(chunk) < 2:
            yield chunk
            return

        mode = chunk[0]
        data = chunk[1]

        if mode == "updates":
            self.transition(self._state.on_updates())
            yield chunk
            return

        # non-"messages" mode — pass through
        if mode != "messages":
            yield chunk
            return

        # data is (message_chunk, metadata_dict)
        if not isinstance(data, (tuple, list)) or len(data) < 2:
            yield chunk
            return

        msg_chunk: Any = data[0]
        metadata: dict[str, Any] = data[1] if isinstance(data[1], dict) else {}

        if metadata.get("langgraph_node") != "model":
            self.transition(self._state.on_non_model_node())
            yield chunk
            return

        async for out in self._state.on_model_chunk(chunk, msg_chunk, metadata):
            yield out

    # ------------------------------------------------------------------
    # Chunk builders
    # ------------------------------------------------------------------
    @staticmethod
    def text_chunk(
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
    def halted_short_circuit_message() -> str:
        """Message yielded when ``_HALTED_KEY`` is already set."""
        return (
            "[Output Repetition Guard] Output repetition was detected "
            "earlier this turn. I must stop here."
        )


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
        except Exception as e:
            logger.debug("[RepetitionGuardWrapper] Command.resume session_id probe failed: {}", e)

        # 3. config configurable fallback
        try:
            cfg = config or {}
            if isinstance(cfg, dict):
                conf = cfg.get("configurable", {})
                if isinstance(conf, dict):
                    sid = conf.get("session_id", "")
                    if sid.strip():
                        return sid
        except Exception as e:
            logger.debug("[RepetitionGuardWrapper] config session_id probe failed: {}", e)

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
    # Streaming interception (astream)
    # ------------------------------------------------------------------
    async def astream(self, *args, **kwargs) -> AsyncGenerator[tuple]:
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

        machine = StreamGuardMachine(
            session_id,
            self._guard,
            self._phantom_stream_guard and isinstance(input_, dict),
        )
        generator = self._inner.astream(*args, **kwargs)

        try:
            async for chunk in generator:
                async for out in machine.on_chunk(chunk):
                    yield out
        finally:
            if machine.phantom_dropped > 0:
                try:
                    logger.critical(
                        "[RepetitionGuardWrapper] PHANTOM stream total: "
                        "dropped {} pre-update model chunk(s) session={}",
                        machine.phantom_dropped,
                        session_id,
                    )
                except Exception as e:
                    logger.debug("[RepetitionGuardWrapper] phantom stream log write failed: {}", e)
            if generator is not None:
                try:
                    await generator.aclose()
                except Exception as e:
                    logger.debug("[RepetitionGuardWrapper] generator aclose failed: {}", e)

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
