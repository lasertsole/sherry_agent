"""ContextLimitGuardWrapper — stream-level context-window guard.

Wraps the compiled agent graph OUTSIDE the RepetitionGuardWrapper and
monitors the ``astream`` chunk flow, closing the Summarization middleware's
streaming blind spots: the middlewares never see mid-stream chunks, and a
post-response overflow signal cannot retroactively compact the context.

Two defenses:

1. **Model-call boundary force-compress** — real ``usage_metadata``
   input/output tokens are captured from the stream and checked against
   ``COMPRESSION_TRIGGER_RATIO`` of the context window. At/over the
   threshold the Summarization anti-thrash gate is forced open for the
   session (``_FORCE_RECOVERY_KEY``) so the next pre-call check compresses
   instead of skipping (cooldown / attempt-cap).
2. **Mid-stream output budget** — accumulated model text is estimated at
   ``_CHARS_PER_TOKEN`` characters per token; once it exceeds
   ``output_cut_ratio`` of the window, further text chunks are no longer
   forwarded to the client and a one-time truncation marker is emitted
   instead. The graph still accumulates the full AIMessage — the truncated
   tail is exactly what the next compression pass removes.

Everything else passes through untouched; ``ainvoke`` delegates (the
Summarization T1-T3 triggers already cover the non-streaming path).
"""

from __future__ import annotations

from typing import Any
from collections.abc import AsyncGenerator

from loguru import logger
from langchain_core.messages import AIMessageChunk
from langgraph.graph.state import CompiledStateGraph

from config.features import CONTEXT_GUARD, SUMMARIZATION, TOKEN_ESTIMATION
from runtime import state_register_mem
from agent.middlewares.summarization_components import _FORCE_RECOVERY_KEY
from .repetition_guard import RepetitionGuardWrapper

COMPRESSION_TRIGGER_RATIO = SUMMARIZATION["compression_trigger_ratio"]
_CHARS_PER_TOKEN = TOKEN_ESTIMATION["chars_per_token"]
_TRUNCATION_MARKER = (
    "[System notice: the response exceeded the mid-stream output budget and was truncated.]"
)


class ContextLimitGuardWrapper:
    """Wrap a ``CompiledStateGraph`` and guard the context window at stream level.

    Parameters
    ----------
    inner:
        The compiled agent graph to wrap (already RepetitionGuard-wrapped).
    context_window:
        The main model's context window in tokens (``MAIN_LLM_MAX_TOKEN``,
        the same source Summarization's trigger uses).
    output_cut_ratio:
        Fraction of the window reserved as the mid-stream output budget.
    check_interval:
        Chunks accumulated between output-budget checks.
    """

    def __init__(
        self,
        inner: CompiledStateGraph,
        context_window: int,
        output_cut_ratio: float = CONTEXT_GUARD["output_cut_ratio"],
        check_interval: int = CONTEXT_GUARD["check_interval"],
    ) -> None:
        self._inner = inner
        self._context_window = context_window
        self._output_token_budget = int(context_window * output_cut_ratio)
        self._check_interval = check_interval

    # ------------------------------------------------------------------
    # Force-compress (defense 1)
    # ------------------------------------------------------------------
    def _check_and_force_compress(
        self, session_id: str, input_tokens: int, output_tokens: int
    ) -> bool:
        """Arm Summarization's forced recovery when reported usage crosses the line.

        Returns True when the flag was set. Both the current-call check
        (``input_tokens`` alone) and the predictive check (``input + output``,
        since the output becomes next call's input) share the same threshold.
        """
        threshold = int(self._context_window * COMPRESSION_TRIGGER_RATIO)
        if input_tokens < threshold and input_tokens + output_tokens < threshold:
            return False
        logger.warning(
            "ContextLimitGuard: reported input_tokens={} (+{} output) reached "
            "threshold {} — forcing Summarization recovery for session {}",
            input_tokens,
            output_tokens,
            threshold,
            session_id,
        )
        state_register_mem.set_state(session_id, _FORCE_RECOVERY_KEY, True)
        return True

    # ------------------------------------------------------------------
    # Streaming interception (astream)
    # ------------------------------------------------------------------
    async def astream(self, *args: Any, **kwargs: Any) -> AsyncGenerator[tuple]:
        """Intercepted streaming: usage capture + force-compress + output budget.

        Accepts the same arguments as ``CompiledStateGraph.astream`` and
        yields the same ``(mode, data)`` chunk format.
        """
        input_ = args[0] if args else kwargs.get("input")
        config = args[1] if len(args) > 1 else kwargs.get("config")
        session_id = RepetitionGuardWrapper._extract_session_id(input_, config)

        last_input_tokens = 0
        last_output_tokens = 0
        call_output_text = ""
        token_counter = 0
        output_cut = False

        async for chunk in self._inner.astream(*args, **kwargs):
            if not (isinstance(chunk, (tuple, list)) and len(chunk) >= 2):
                yield chunk
                continue

            mode, data = chunk[0], chunk[1]

            if mode == "messages":
                if not (isinstance(data, (tuple, list)) and len(data) >= 2):
                    yield chunk
                    continue
                msg_chunk: Any = data[0]
                metadata: dict[str, Any] = data[1] if isinstance(data[1], dict) else {}

                if metadata.get("langgraph_node") != "model":
                    # Non-model node: the previous model call is over —
                    # flush the per-call budget tracking.
                    call_output_text = ""
                    token_counter = 0
                    output_cut = False
                    yield chunk
                    continue

                content = getattr(msg_chunk, "content", None)
                if content and isinstance(content, str) and not output_cut:
                    call_output_text += content
                    token_counter += 1
                    if token_counter >= self._check_interval:
                        token_counter = 0
                        if len(call_output_text) // _CHARS_PER_TOKEN > self._output_token_budget:
                            output_cut = True
                            logger.warning(
                                "ContextLimitGuard: mid-stream output budget "
                                "({} est. tokens > {}) exceeded for session {} — "
                                "truncating client view",
                                len(call_output_text) // _CHARS_PER_TOKEN,
                                self._output_token_budget,
                                session_id,
                            )
                            yield (
                                "messages",
                                (AIMessageChunk(content=_TRUNCATION_MARKER), metadata),
                            )

                usage = getattr(msg_chunk, "usage_metadata", None)
                if isinstance(usage, dict):
                    if usage.get("input_tokens") is not None:
                        last_input_tokens = int(usage["input_tokens"])
                    if usage.get("output_tokens") is not None:
                        last_output_tokens = int(usage["output_tokens"])

                # Suppress further text chunks; tool-call chunks still pass
                # (tool functionality must not be affected by the cut).
                if output_cut and content and not getattr(msg_chunk, "tool_call_chunks", None):
                    continue

                yield chunk
                continue

            if mode == "updates":
                # Model-call boundary: check real usage, reset the budget.
                self._check_and_force_compress(session_id, last_input_tokens, last_output_tokens)
                call_output_text = ""
                token_counter = 0
                output_cut = False
                yield chunk
                continue

            yield chunk

        # Stream end: flush the final model call's usage.
        self._check_and_force_compress(session_id, last_input_tokens, last_output_tokens)

    # ------------------------------------------------------------------
    # Non-streaming (ainvoke)
    # ------------------------------------------------------------------
    async def ainvoke(self, *args: Any, **kwargs: Any) -> Any:
        """Delegate to the inner agent (T1-T3 cover the non-streaming path)."""
        return await self._inner.ainvoke(*args, **kwargs)

    # ------------------------------------------------------------------
    # Transparent delegation
    # ------------------------------------------------------------------
    def __getattr__(self, name: str) -> Any:
        """Delegate unknown attribute access to the inner agent."""
        return getattr(self._inner, name)

    @property
    def inner(self) -> CompiledStateGraph:
        """The wrapped inner agent graph."""
        return self._inner
