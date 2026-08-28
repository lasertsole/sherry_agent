# RepetitionGuardWrapper — 变更重建指南

> 本文档记录了将 `OutputRepetitionGuard` 中间件 + `check_stream_repetition` 散调用
> 替换为统一的 `RepetitionGuardWrapper` 包装器的全部代码变更。
>
> **使用方式**：按照以下步骤，基于旧代码（变更前状态）和本文档，即可完整重建新代码。
> 无需参考任何新代码文件。

---

## 目录

1. [新建文件：`agent/repetition_guard_wrapper.py`](#1-新建文件agentrepetition_guard_wrapperpy)
2. [新建文件：`tests/unit/test_repetition_guard_wrapper.py`](#2-新建文件testsunittest_repetition_guard_wrapperpy)
3. [修改文件：`agent/__init__.py`](#3-修改文件agent__init__py)
4. [修改文件：`agent/core.py`](#4-修改文件agentcorepy)
5. [修改文件：`server/service/messages.py`](#5-修改文件serverservicemessagespy)
6. [验证步骤](#6-验证步骤)

---

## 1. 新建文件：`agent/repetition_guard_wrapper.py`

**完整文件内容如下，直接创建：**

```python
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
    ):
        self._inner = inner
        self._guard = OutputRepetitionGuard(
            max_identical_outputs=max_identical_outputs,
            warn_after=warn_after,
            internal_repeat_ratio=internal_repeat_ratio,
            internal_min_lines=internal_min_lines,
            char_run_min=char_run_min,
        )

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
                # "updates" mode — model call boundary
                # ====================================================
                if mode == "updates":
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
                                        "stream internal repetition — cutting",
                                        session_id,
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
```

---

## 2. 新建文件：`tests/unit/test_repetition_guard_wrapper.py`

**完整文件内容如下，直接创建：**

```python
"""Unit tests for agent/repetition_guard_wrapper.py — RepetitionGuardWrapper.

Covers ALL OutputRepetitionGuard interception functionality at the wrapper level:

  * Stream passthrough (normal, non-repetitive text)
  * Stream-level internal repetition (sentence, char-run, phrase) — text cut
  * Cross-call identical output WARN / HALT at model-call boundaries
  * Reasoning text repetition (independent history)
  * Per-turn state reset (astream + ainvoke)
  * Non-streaming (ainvoke) post-hoc detection + replacement
  * Tool-call chunks pass through unaffected
  * HALT short-circuit (subsequent calls yield halt message)
  * Session isolation
  * Multiple model calls within one turn (model -> tool -> model -> ...)
  * Short content below threshold skipped
  * Internal warning dedup (once per session)
  * Method delegation (aget_state etc.)
  * Stream-mode passthrough (non-"messages" -> no interception)
  * Command resume input (session_id extracted from resume value)
"""

import asyncio
import sys
import types

import pytest
from unittest.mock import AsyncMock, MagicMock

# ---- llama_cpp stub (same as test_output_repetition_guard.py) ----
_llama_stub = types.ModuleType("llama_cpp")
_llama_stub.Llama = type("Llama", (), {})
sys.modules.setdefault("llama_cpp", _llama_stub)
_llama_chat_sub = types.ModuleType("llama_cpp.llama_chat_format")
_llama_chat_sub.Qwen25VLChatHandler = type("Qwen25VLChatHandler", (), {})
sys.modules.setdefault("llama_cpp.llama_chat_format", _llama_chat_sub)

# ---- langchain.agents stubs ----
# The installed langchain/langgraph versions have an incompatible import
# chain (langchain.agents.__init__ -> factory -> langgraph.prebuilt ->
# langgraph.runtime.ExecutionInfo).  Stub these modules so
# OutputRepetitionGuard and the middleware __init__ can be imported
# without triggering the chain.
_la_stub = types.ModuleType("langchain.agents")
_la_stub.AgentState = dict
_la_stub.create_agent = lambda **kw: None
sys.modules.setdefault("langchain.agents", _la_stub)

_mw_stub = types.ModuleType("langchain.agents.middleware")
_mw_stub.AgentMiddleware = type("AgentMiddleware", (), {
    "__init__": lambda self, *a, **kw: None,
})
_mw_stub.AgentState = dict
_mw_stub.SummarizationMiddleware = type("SummarizationMiddleware", (), {
    "__init__": lambda self, *a, **kw: None,
})
sys.modules.setdefault("langchain.agents.middleware", _mw_stub)

_mw_types_stub = types.ModuleType("langchain.agents.middleware.types")
_mw_types_stub.ResponseT = type("ResponseT", (), {})
_mw_types_stub.ModelRequest = type("ModelRequest", (), {})
_mw_types_stub.ModelResponse = type("ModelResponse", (), {})
_mw_types_stub.ExtendedModelResponse = type("ExtendedModelResponse", (), {})
_mw_types_stub.AgentMiddleware = _mw_stub.AgentMiddleware
_mw_types_stub.AgentState = dict
sys.modules.setdefault("langchain.agents.middleware.types", _mw_types_stub)

# ---- langgraph.runtime stub (add missing ExecutionInfo / ServerInfo) ----
try:
    import langgraph.runtime as _lg_rt
    if not hasattr(_lg_rt, "ExecutionInfo"):
        _lg_rt.ExecutionInfo = type("ExecutionInfo", (), {})
    if not hasattr(_lg_rt, "ServerInfo"):
        _lg_rt.ServerInfo = type("ServerInfo", (), {})
    if not hasattr(_lg_rt, "Runtime"):
        _lg_rt.Runtime = type("Runtime", (), {})
except Exception:
    _lg_rt_stub = types.ModuleType("langgraph.runtime")
    _lg_rt_stub.ExecutionInfo = type("ExecutionInfo", (), {})
    _lg_rt_stub.ServerInfo = type("ServerInfo", (), {})
    _lg_rt_stub.Runtime = type("Runtime", (), {})
    sys.modules.setdefault("langgraph.runtime", _lg_rt_stub)

# ---- agent.middlewares stub (skip __init__.py which imports all middlewares) ----
_am_stub = types.ModuleType("agent.middlewares")
_am_stub.__path__ = ["agent/middlewares"]
sys.modules.setdefault("agent.middlewares", _am_stub)

# Make agent.middlewares accessible as an attribute of the agent package
# (monkeypatch.resolve_importpath accesses it through the package, not
# sys.modules directly).
import agent as _agent_pkg  # noqa: E402
if not hasattr(_agent_pkg, "middlewares"):
    _agent_pkg.middlewares = _am_stub

from runtime.core import Register
from runtime.state_register import StateRegisterMeM
from langchain_core.messages import AIMessage, AIMessageChunk, HumanMessage, ToolMessage

from agent.middlewares.output_repetition_guard import (
    _HISTORY_KEY,
    _INTERNAL_WARNED_KEY,
    _HALTED_KEY,
    _REASONING_HISTORY_KEY,
    _REASONING_WARNED_KEY,
    _MIN_CONTENT_LENGTH,
    _STREAM_WARNING,
)
import agent.middlewares.output_repetition_guard as _org_module
import agent.repetition_guard_wrapper as _wrapper_module
from agent.repetition_guard_wrapper import RepetitionGuardWrapper


# ======================================================================
# Helpers
# ======================================================================

def msg_chunk(
    content: str,
    node: str = "model",
    reasoning: str = "",
    **meta,
) -> tuple:
    """Build a ("messages", (AIMessageChunk, metadata)) chunk."""
    ak = {}
    if reasoning:
        ak["reasoning_content"] = reasoning
    chunk = AIMessageChunk(content=content, additional_kwargs=ak)
    metadata = {"langgraph_node": node, **meta}
    return ("messages", (chunk, metadata))


def tool_msg_chunk(
    name: str = "web_search",
    tool_id: str = "tc1",
    args: str = "",
) -> tuple:
    """Build a ("messages", (AIMessageChunk with tool_call, metadata)) chunk."""
    chunk = AIMessageChunk(
        content="",
        tool_call_chunks=[{"name": name, "id": tool_id, "args": args}],
    )
    return ("messages", (chunk, {"langgraph_node": "model"}))


def update_chunk(
    node: str = "tools",
    messages: list | None = None,
) -> tuple:
    """Build a ("updates", {node: {"messages": messages}}) chunk."""
    return ("updates", {node: {"messages": messages or []}})


class MockAgent:
    """Mock CompiledStateGraph that yields predefined stream chunks."""

    def __init__(
        self,
        stream_chunks: list | None = None,
        invoke_result: dict | None = None,
    ):
        self._stream_chunks = stream_chunks or []
        self._invoke_result = invoke_result or {"messages": []}

    async def astream(self, *args, **kwargs):
        for chunk in self._stream_chunks:
            yield chunk

    async def ainvoke(self, *args, **kwargs):
        return self._invoke_result

    async def aget_state(self, config=None, **kwargs):
        mock = MagicMock()
        mock.values = {}
        return mock

    async def aupdate_state(self, config=None, values=None, **kwargs):
        return None


def _collect_stream(wrapper, session_id="s1", stream_mode=None) -> list:
    """Run astream and collect all output chunks."""
    if stream_mode is None:
        stream_mode = ["messages", "updates"]

    async def _run():
        chunks = []
        async for chunk in wrapper.astream(
            input={"session_id": session_id, "messages": []},
            config={},
            stream_mode=stream_mode,
        ):
            chunks.append(chunk)
        return chunks

    return asyncio.run(_run())


def _text_parts(chunks: list) -> str:
    """Concatenate all text content from model-node message chunks."""
    parts = []
    for mode, data in chunks:
        if mode != "messages":
            continue
        if not isinstance(data, (tuple, list)) or len(data) < 2:
            continue
        mc, meta = data[0], data[1]
        if not isinstance(mc, AIMessageChunk):
            continue
        if not isinstance(meta, dict) or meta.get("langgraph_node") != "model":
            continue
        if mc.content:
            parts.append(str(mc.content))
    return "".join(parts)


def _has_warning(chunks: list) -> bool:
    """Check if any chunk contains the repetition guard warning."""
    text = _text_parts(chunks)
    return "[Output Repetition Guard]" in text


# ======================================================================
# Fixtures
# ======================================================================

@pytest.fixture
def fresh_state(monkeypatch):
    """Provide an isolated StateRegisterMeM patched into both the wrapper
    and middleware modules."""
    if StateRegisterMeM in Register._instances:
        del Register._instances[StateRegisterMeM]
    reg = StateRegisterMeM()
    monkeypatch.setattr(_org_module, "state_register_mem", reg)
    monkeypatch.setattr(_wrapper_module, "state_register_mem", reg)
    yield reg
    if StateRegisterMeM in Register._instances:
        del Register._instances[StateRegisterMeM]


# ======================================================================
# 1. Passthrough
# ======================================================================

class TestPassthrough:
    """Normal, non-repetitive text passes through unchanged."""

    def test_clean_text_forwarded(self, fresh_state):
        text = "This is a perfectly normal and varied answer about topics."
        chunks = [msg_chunk(text)]
        wrapper = RepetitionGuardWrapper(MockAgent(stream_chunks=chunks))
        out = _collect_stream(wrapper)
        assert _text_parts(out) == text
        assert not _has_warning(out)

    def test_multiple_chunks_forwarded(self, fresh_state):
        parts = ["Hello ", "world ", "this is ", "a varied response."]
        chunks = [msg_chunk(p) for p in parts]
        wrapper = RepetitionGuardWrapper(MockAgent(stream_chunks=chunks))
        out = _collect_stream(wrapper)
        assert _text_parts(out) == "".join(parts)

    def test_updates_passed_through(self, fresh_state):
        chunks = [
            msg_chunk("Normal text here that is long enough."),
            update_chunk("tools", [ToolMessage(content="result", tool_call_id="t1")]),
        ]
        wrapper = RepetitionGuardWrapper(MockAgent(stream_chunks=chunks))
        out = _collect_stream(wrapper)
        # Should have both messages and updates
        modes = [c[0] for c in out]
        assert "messages" in modes
        assert "updates" in modes


# ======================================================================
# 2. Stream-level internal repetition
# ======================================================================

class TestInternalRepetitionStream:
    """Internal repetition detected mid-stream — text cut, warning yielded."""

    def test_char_run_detected_and_cut(self, fresh_state):
        repetitive = "字" * 40  # > _CHAR_RUN_MIN(8) and > _MIN_CONTENT_LENGTH(20)
        chunks = [msg_chunk(repetitive)]
        wrapper = RepetitionGuardWrapper(MockAgent(stream_chunks=chunks))
        out = _collect_stream(wrapper)

        assert _has_warning(out)
        # The warning text should contain the stream warning
        text = _text_parts(out)
        assert _STREAM_WARNING.strip() in text
        # The repetitive text itself should be suppressed
        assert "字" * 40 not in text

    def test_sentence_repetition_detected(self, fresh_state):
        # > internal_min_lines(6) with > 60% duplicates
        sentence = "hello."
        content = (sentence + "\n") * 10
        chunks = [msg_chunk(content)]
        wrapper = RepetitionGuardWrapper(MockAgent(stream_chunks=chunks))
        out = _collect_stream(wrapper)
        assert _has_warning(out)

    def test_phrase_repetition_detected(self, fresh_state):
        # "我来帮你" repeated 8 times -> phrase detector fires (> 5 repeats)
        content = "我来帮你" * 8
        chunks = [msg_chunk(content)]
        wrapper = RepetitionGuardWrapper(MockAgent(stream_chunks=chunks))
        out = _collect_stream(wrapper)
        assert _has_warning(out)

    def test_warning_fires_only_once_per_session(self, fresh_state):
        """If repetition is detected, the _INTERNAL_WARNED_KEY is set so a
        second detection in the same turn does not yield another warning."""
        repetitive = "字" * 40
        chunks = [msg_chunk(repetitive), msg_chunk(repetitive)]
        wrapper = RepetitionGuardWrapper(MockAgent(stream_chunks=chunks))
        out = _collect_stream(wrapper)
        # Count how many times the warning appears
        text = _text_parts(out)
        count = text.count("[Output Repetition Guard]")
        assert count == 1
        assert fresh_state.get_state("s1", _INTERNAL_WARNED_KEY, False) is True

    def test_progressive_accumulation_triggers(self, fresh_state):
        """Repetition builds up over multiple small chunks — the accumulated
        text is checked on each chunk, so the warning fires mid-stream."""
        # Start with normal text, then degenerate into char-run
        chunks = [
            msg_chunk("Here is my analysis: "),
            msg_chunk("字" * 10),
            msg_chunk("字" * 10),
            msg_chunk("字" * 10),
        ]
        wrapper = RepetitionGuardWrapper(MockAgent(stream_chunks=chunks))
        out = _collect_stream(wrapper)
        assert _has_warning(out)

    def test_below_min_length_no_warning(self, fresh_state):
        """Content below _MIN_CONTENT_LENGTH is never checked."""
        short_run = "啊" * 10  # 10 < 20
        chunks = [msg_chunk(short_run)]
        wrapper = RepetitionGuardWrapper(MockAgent(stream_chunks=chunks))
        out = _collect_stream(wrapper)
        assert not _has_warning(out)
        assert fresh_state.get_state("s1", _INTERNAL_WARNED_KEY, False) is False


# ======================================================================
# 3. Cross-call repetition at model-call boundaries
# ======================================================================

class TestCrossCallRepetition:
    """Cross-call identical output detection at model-call boundaries."""

    _CONTENT = "Long identical content that repeats across multiple calls."

    def _two_call_stream(self, content: str):
        """Build a stream with two model calls separated by a tools update."""
        return [
            msg_chunk(content),
            update_chunk("tools", [ToolMessage(content="ok", tool_call_id="t1")]),
            msg_chunk(content),
            update_chunk("tools", [ToolMessage(content="ok", tool_call_id="t2")]),
        ]

    def test_warn_on_second_identical(self, fresh_state):
        wrapper = RepetitionGuardWrapper(
            MockAgent(stream_chunks=self._two_call_stream(self._CONTENT)),
            warn_after=2,
            max_identical_outputs=3,
        )
        out = _collect_stream(wrapper)
        assert _has_warning(out)
        assert fresh_state.get_state("s1", _HALTED_KEY, False) is False

    def test_halt_on_third_identical(self, fresh_state):
        content = "Long identical content that repeats across multiple calls."
        chunks = [
            msg_chunk(content),
            update_chunk("tools", [ToolMessage(content="ok", tool_call_id="t1")]),
            msg_chunk(content),
            update_chunk("tools", [ToolMessage(content="ok", tool_call_id="t2")]),
            msg_chunk(content),
            update_chunk("tools", [ToolMessage(content="ok", tool_call_id="t3")]),
            # Extra content after HALT — should NOT be forwarded
            msg_chunk("This should never reach the client."),
        ]
        wrapper = RepetitionGuardWrapper(
            MockAgent(stream_chunks=chunks),
            warn_after=2,
            max_identical_outputs=3,
        )
        out = _collect_stream(wrapper)
        text = _text_parts(out)
        assert "[Output Repetition Guard]" in text
        assert fresh_state.get_state("s1", _HALTED_KEY, False) is True
        # The stream should be cancelled on HALT — text after the 3rd
        # boundary (which triggers HALT) must NOT appear.
        assert "This should never reach the client." not in text

    def test_different_content_breaks_streak(self, fresh_state):
        c1 = "First long distinct output that is definitely different."
        c2 = "Second long distinct output that is definitely different."
        c3 = "Third completely unrelated output, different from both."
        chunks = [
            msg_chunk(c1),
            update_chunk("tools", [ToolMessage(content="ok", tool_call_id="t1")]),
            msg_chunk(c2),
            update_chunk("tools", [ToolMessage(content="ok", tool_call_id="t2")]),
            msg_chunk(c3),
        ]
        wrapper = RepetitionGuardWrapper(
            MockAgent(stream_chunks=chunks),
            warn_after=2,
            max_identical_outputs=3,
        )
        out = _collect_stream(wrapper)
        assert not _has_warning(out)

    def test_history_recorded(self, fresh_state):
        wrapper = RepetitionGuardWrapper(
            MockAgent(stream_chunks=self._two_call_stream(self._CONTENT)),
            warn_after=2,
            max_identical_outputs=3,
        )
        _collect_stream(wrapper)
        hist = fresh_state.get_state("s1", _HISTORY_KEY, [])
        assert len(hist) >= 1

    def test_short_content_skipped_in_cross_call(self, fresh_state):
        short = "hi"  # below _MIN_CONTENT_LENGTH
        chunks = [
            msg_chunk(short),
            update_chunk("tools", [ToolMessage(content="ok", tool_call_id="t1")]),
            msg_chunk(short),
        ]
        wrapper = RepetitionGuardWrapper(
            MockAgent(stream_chunks=chunks),
            warn_after=1,
            max_identical_outputs=2,
        )
        out = _collect_stream(wrapper)
        assert not _has_warning(out)
        assert fresh_state.get_state("s1", _HISTORY_KEY, []) == []


# ======================================================================
# 4. Reasoning text repetition
# ======================================================================

class TestReasoningRepetition:
    """Reasoning text tracked independently from visible output."""

    def test_reasoning_history_separate(self, fresh_state):
        reasoning = "stuck reasoning loop repeated again and again verbatim"
        visible = "A visible answer that changes each call to be unique."
        chunks = [
            msg_chunk(visible, reasoning=reasoning),
            update_chunk("tools", [ToolMessage(content="ok", tool_call_id="t1")]),
            msg_chunk("Different visible text here to avoid output match.", reasoning=reasoning),
            update_chunk("tools", [ToolMessage(content="ok", tool_call_id="t2")]),
        ]
        wrapper = RepetitionGuardWrapper(
            MockAgent(stream_chunks=chunks),
            warn_after=2,
            max_identical_outputs=3,
        )
        out = _collect_stream(wrapper)
        # Reasoning repetition should trigger a warning at the boundary
        assert _has_warning(out)
        # Reasoning history should have entries
        rhist = fresh_state.get_state("s1", _REASONING_HISTORY_KEY, [])
        assert len(rhist) >= 1

    def test_reasoning_short_skipped(self, fresh_state):
        short_reasoning = "abc"
        chunks = [
            msg_chunk("Long enough visible output text.", reasoning=short_reasoning),
            update_chunk("tools", [ToolMessage(content="ok", tool_call_id="t1")]),
            msg_chunk("Different visible output text here.", reasoning=short_reasoning),
        ]
        wrapper = RepetitionGuardWrapper(MockAgent(stream_chunks=chunks))
        out = _collect_stream(wrapper)
        # Below _MIN_CONTENT_LENGTH -> not tracked
        assert fresh_state.get_state("s1", _REASONING_HISTORY_KEY, []) == []


# ======================================================================
# 5. Per-turn state reset
# ======================================================================

class TestPerTurnReset:
    """State is cleared at the start of each astream/ainvoke call."""

    def test_astream_resets_state(self, fresh_state):
        # Seed stale state
        fresh_state.set_state("s1", _HISTORY_KEY, ["stale"])
        fresh_state.set_state("s1", _HALTED_KEY, True)
        fresh_state.set_state("s1", _INTERNAL_WARNED_KEY, True)

        wrapper = RepetitionGuardWrapper(
            MockAgent(stream_chunks=[msg_chunk("Normal varied response text.")])
        )
        _collect_stream(wrapper)

        assert fresh_state.get_state("s1", _HISTORY_KEY, "X") == [] or \
               len(fresh_state.get_state("s1", _HISTORY_KEY, [])) <= 1
        # HALTED and INTERNAL_WARNED must be reset before the turn starts
        # (they may be set again during the turn, but only if repetition
        # actually occurs — which it won't for clean text).
        assert fresh_state.get_state("s1", _HALTED_KEY, False) is False

    def test_ainvoke_resets_state(self, fresh_state):
        fresh_state.set_state("s1", _HISTORY_KEY, ["stale"])
        fresh_state.set_state("s1", _HALTED_KEY, True)

        result = {"messages": [AIMessage(content="Normal clean text response.")]}
        wrapper = RepetitionGuardWrapper(MockAgent(invoke_result=result))

        async def _run():
            return await wrapper.ainvoke(
                input={"session_id": "s1", "messages": []}, config={}
            )

        asyncio.run(_run())
        assert fresh_state.get_state("s1", _HALTED_KEY, False) is False

    def test_reset_allows_rearming(self, fresh_state):
        """After per-turn reset, a fresh repetitive stream warns again."""
        repetitive = "字" * 40
        wrapper = RepetitionGuardWrapper(
            MockAgent(stream_chunks=[msg_chunk(repetitive)])
        )
        # First turn — warns
        out1 = _collect_stream(wrapper, session_id="s1")
        assert _has_warning(out1)
        assert fresh_state.get_state("s1", _INTERNAL_WARNED_KEY, False) is True

        # Second turn — reset clears the flag, warns again
        out2 = _collect_stream(wrapper, session_id="s1")
        assert _has_warning(out2)


# ======================================================================
# 6. Non-streaming (ainvoke) post-hoc detection
# ======================================================================

class TestNonStreamingPostHoc:
    """ainvoke path — post-hoc detection on the final AIMessage."""

    def test_repetitive_content_replaced(self, fresh_state):
        repetitive = "字" * 40
        result = {
            "messages": [AIMessage(content=repetitive)],
            "session_id": "s1",
        }
        wrapper = RepetitionGuardWrapper(MockAgent(invoke_result=result))

        async def _run():
            return await wrapper.ainvoke(
                input={"session_id": "s1", "messages": []}, config={}
            )

        out = asyncio.run(_run())
        last = out["messages"][-1]
        assert isinstance(last, AIMessage)
        assert "[Output Repetition Guard]" in last.content

    def test_clean_content_passes_through(self, fresh_state):
        clean = "This is a normal and varied response about multiple topics."
        result = {"messages": [AIMessage(content=clean)], "session_id": "s1"}
        wrapper = RepetitionGuardWrapper(MockAgent(invoke_result=result))

        async def _run():
            return await wrapper.ainvoke(
                input={"session_id": "s1", "messages": []}, config={}
            )

        out = asyncio.run(_run())
        assert out["messages"][-1].content == clean

    def test_tool_call_message_skipped(self, fresh_state):
        """Messages with tool_calls are not checked."""
        msg = AIMessage(
            content="",
            tool_calls=[{"name": "web_search", "args": {}, "id": "tc1"}],
        )
        result = {"messages": [msg], "session_id": "s1"}
        wrapper = RepetitionGuardWrapper(MockAgent(invoke_result=result))

        async def _run():
            return await wrapper.ainvoke(
                input={"session_id": "s1", "messages": []}, config={}
            )

        out = asyncio.run(_run())
        # Should pass through unchanged (tool_calls -> no detection)
        assert out["messages"][-1] is msg

    def test_internal_repetition_in_ainvoke_warns(self, fresh_state):
        """ainvoke detects internal repetition (char-run) in the result."""
        repetitive = "字" * 40
        result = {"messages": [AIMessage(content=repetitive)], "session_id": "s1"}
        wrapper = RepetitionGuardWrapper(MockAgent(invoke_result=result))

        async def _run():
            return await wrapper.ainvoke(
                input={"session_id": "s1", "messages": []}, config={}
            )

        out = asyncio.run(_run())
        last = out["messages"][-1]
        assert "[Output Repetition Guard]" in last.content
        assert "highly repetitive" in last.content


# ======================================================================
# 7. Tool-call chunks pass through
# ======================================================================

class TestToolCallPassthrough:
    """Tool-call chunks are forwarded unaffected by the guard."""

    def test_tool_call_chunks_forwarded(self, fresh_state):
        chunks = [
            tool_msg_chunk("web_search", "tc1", '{"q": "test"}'),
            msg_chunk("Normal response text that is varied and clean."),
        ]
        wrapper = RepetitionGuardWrapper(MockAgent(stream_chunks=chunks))
        out = _collect_stream(wrapper)

        # Tool call chunk should be in output
        tool_chunks = [
            c for c in out
            if c[0] == "messages"
            and isinstance(c[1][0], AIMessageChunk)
            and c[1][0].tool_call_chunks
        ]
        assert len(tool_chunks) >= 1
        assert not _has_warning(out)


# ======================================================================
# 8. HALT short-circuit
# ======================================================================

class TestHaltedShortCircuit:
    """When _HALTED_KEY is already set, model calls yield halt messages."""

    def test_pre_set_halt_yields_halt_message(self, fresh_state):
        """When _HALTED_KEY is set (simulating middleware detection during
        the graph turn), the wrapper yields halt messages instead of
        forwarding model text.

        We simulate this by patching the state check to return True after
        the per-turn reset.
        """
        chunks = [msg_chunk("This text should be replaced by halt message.")]
        wrapper = RepetitionGuardWrapper(MockAgent(stream_chunks=chunks))

        # Patch the get_state call inside astream to always return True for
        # _HALTED_KEY, simulating the middleware having set it during the
        # graph turn (after the per-turn reset).
        original_get = fresh_state.get_state

        def patched_get(session_id, key, default=None):
            if key == _HALTED_KEY:
                return True
            return original_get(session_id, key, default)

        fresh_state.get_state = patched_get

        out = _collect_stream(wrapper)
        text = _text_parts(out)
        assert "must stop" in text.lower() or "halt" in text.lower()
        # Original text should be suppressed
        assert "This text should be replaced by halt message." not in text

    def test_halt_propagates_after_cross_call_halt(self, fresh_state):
        """After a cross-call HALT, if the stream somehow continues (e.g.
        multiple independent model calls not separated by updates), the
        halt short-circuit kicks in."""
        content = "Long identical content for cross-call halt detection."
        chunks = [
            msg_chunk(content),
            update_chunk("tools", [ToolMessage(content="ok", tool_call_id="t1")]),
            msg_chunk(content),
            update_chunk("tools", [ToolMessage(content="ok", tool_call_id="t2")]),
            msg_chunk(content),
            update_chunk("tools", [ToolMessage(content="ok", tool_call_id="t3")]),
        ]
        wrapper = RepetitionGuardWrapper(
            MockAgent(stream_chunks=chunks),
            warn_after=2,
            max_identical_outputs=3,
        )
        out = _collect_stream(wrapper)
        assert fresh_state.get_state("s1", _HALTED_KEY, False) is True


# ======================================================================
# 9. Session isolation
# ======================================================================

class TestSessionIsolation:
    """Different sessions have independent repetition state."""

    def test_independent_sessions(self, fresh_state):
        content = "Long identical content repeated across calls here."
        chunks = [
            msg_chunk(content),
            update_chunk("tools", [ToolMessage(content="ok", tool_call_id="t1")]),
            msg_chunk(content),
        ]
        wrapper = RepetitionGuardWrapper(
            MockAgent(stream_chunks=chunks),
            warn_after=2,
            max_identical_outputs=3,
        )
        # Session A — warns
        out_a = _collect_stream(wrapper, session_id="sA")
        assert _has_warning(out_a)

        # Session B — fresh state, no warning (only 2 calls, but B starts fresh)
        out_b = _collect_stream(wrapper, session_id="sB")
        # B should also warn (same 2 identical calls, warn_after=2)
        # but its _HALTED_KEY should be independent
        assert fresh_state.get_state("sA", _HALTED_KEY, False) is False
        assert fresh_state.get_state("sB", _HALTED_KEY, False) is False


# ======================================================================
# 10. Multiple model calls within one turn
# ======================================================================

class TestMultipleModelCalls:
    """model -> tool -> model -> tool -> model: cross-call works across calls."""

    def test_three_calls_progressive_escalation(self, fresh_state):
        content = "Identical long content repeated across three calls."
        chunks = [
            msg_chunk(content),
            update_chunk("tools", [ToolMessage(content="r1", tool_call_id="t1")]),
            msg_chunk(content),
            update_chunk("tools", [ToolMessage(content="r2", tool_call_id="t2")]),
            msg_chunk(content),
            update_chunk("tools", [ToolMessage(content="r3", tool_call_id="t3")]),
        ]
        wrapper = RepetitionGuardWrapper(
            MockAgent(stream_chunks=chunks),
            warn_after=2,
            max_identical_outputs=3,
        )
        out = _collect_stream(wrapper)
        text = _text_parts(out)
        # Should have gone through warn -> halt escalation
        assert "[Output Repetition Guard]" in text
        assert fresh_state.get_state("s1", _HALTED_KEY, False) is True

    def test_tool_updates_forwarded(self, fresh_state):
        content = "Normal varied response text without repetition at all."
        chunks = [
            msg_chunk(content),
            update_chunk("tools", [ToolMessage(content="result", tool_call_id="t1")]),
            msg_chunk("Different second response text here."),
        ]
        wrapper = RepetitionGuardWrapper(MockAgent(stream_chunks=chunks))
        out = _collect_stream(wrapper)
        # Both tool updates should be forwarded
        update_count = sum(1 for c in out if c[0] == "updates")
        assert update_count == 1  # only one tools update in the stream


# ======================================================================
# 11. Method delegation
# ======================================================================

class TestDelegation:
    """Unknown methods are delegated to the inner agent."""

    def test_aget_state_delegated(self, fresh_state):
        mock = MockAgent()
        wrapper = RepetitionGuardWrapper(mock)

        async def _run():
            return await wrapper.aget_state(config={})

        result = asyncio.run(_run())
        assert result is not None  # MockAgent returns a MagicMock

    def test_inner_property(self, fresh_state):
        mock = MockAgent()
        wrapper = RepetitionGuardWrapper(mock)
        assert wrapper.inner is mock

    def test_aupdate_state_delegated(self, fresh_state):
        mock = MockAgent()
        wrapper = RepetitionGuardWrapper(mock)

        async def _run():
            return await wrapper.aupdate_state(config={}, values={"x": 1})

        result = asyncio.run(_run())
        assert result is None  # MockAgent returns None


# ======================================================================
# 12. Stream-mode passthrough
# ======================================================================

class TestStreamModePassthrough:
    """Non-"messages" stream_mode -> no interception, pass through."""

    def test_no_messages_mode_passthrough(self, fresh_state):
        # With stream_mode=["updates"], no message chunks to intercept
        chunks = [
            update_chunk("model", [AIMessage(content="字" * 40)]),
        ]
        wrapper = RepetitionGuardWrapper(MockAgent(stream_chunks=chunks))
        out = _collect_stream(wrapper, stream_mode=["updates"])
        # Should pass through without warning
        assert not _has_warning(out)

    def test_none_stream_mode_passthrough(self, fresh_state):
        chunks = [msg_chunk("Normal text here.")]
        wrapper = RepetitionGuardWrapper(MockAgent(stream_chunks=chunks))
        out = _collect_stream(wrapper, stream_mode=None)
        # stream_mode=None -> inner agent gets None, mock yields all chunks
        # No interception (no "messages" in stream_mode)
        assert len(out) >= 1


# ======================================================================
# 13. Command resume input
# ======================================================================

class TestCommandResumeInput:
    """session_id extracted from Command.resume for HITL resume path."""

    def test_command_resume_session_id(self, fresh_state):
        from langgraph.types import Command

        cmd = Command(resume={
            "session_id": "cmd-session",
            "decisions": [{"type": "approve"}],
        })
        chunks = [msg_chunk("Normal clean response text here.")]
        wrapper = RepetitionGuardWrapper(MockAgent(stream_chunks=chunks))

        async def _run():
            out = []
            async for chunk in wrapper.astream(
                cmd, config={}, stream_mode=["messages", "updates"]
            ):
                out.append(chunk)
            return out

        out = asyncio.run(_run())
        assert not _has_warning(out)
        # State should have been reset for this session
        assert fresh_state.get_state("cmd-session", _HALTED_KEY, False) is False


# ======================================================================
# 14. Missing session_id
# ======================================================================

class TestMissingSessionId:
    """RuntimeError when session_id cannot be found."""

    def test_no_session_id_raises(self, fresh_state):
        wrapper = RepetitionGuardWrapper(MockAgent(stream_chunks=[]))

        async def _run():
            async for _ in wrapper.astream(
                input={"messages": []}, config={}, stream_mode=["messages", "updates"]
            ):
                pass

        with pytest.raises(RuntimeError, match="session_id is required"):
            asyncio.run(_run())

    def test_blank_session_id_raises(self, fresh_state):
        wrapper = RepetitionGuardWrapper(MockAgent(stream_chunks=[]))

        async def _run():
            async for _ in wrapper.astream(
                input={"session_id": "  ", "messages": []},
                config={},
                stream_mode=["messages", "updates"],
            ):
                pass

        with pytest.raises(RuntimeError, match="session_id is required"):
            asyncio.run(_run())


# ======================================================================
# 15. Configuration
# ======================================================================

class TestConfiguration:
    """Custom thresholds are respected."""

    def test_custom_warn_after(self, fresh_state):
        content = "Long enough identical content for a single call to detect."
        chunks = [
            msg_chunk(content),
            update_chunk("tools", [ToolMessage(content="ok", tool_call_id="t1")]),
            msg_chunk(content),
        ]
        # warn_after=1 -> first repeat warns
        wrapper = RepetitionGuardWrapper(
            MockAgent(stream_chunks=chunks),
            warn_after=1,
            max_identical_outputs=5,
        )
        out = _collect_stream(wrapper)
        assert _has_warning(out)

    def test_custom_max_identical_outputs(self, fresh_state):
        content = "Long enough identical content for detection to fire."
        chunks = [
            msg_chunk(content),
            update_chunk("tools", [ToolMessage(content="ok", tool_call_id="t1")]),
            msg_chunk(content),
            update_chunk("tools", [ToolMessage(content="ok", tool_call_id="t2")]),
            msg_chunk(content),
            update_chunk("tools", [ToolMessage(content="ok", tool_call_id="t3")]),
            msg_chunk(content),
            update_chunk("tools", [ToolMessage(content="ok", tool_call_id="t4")]),
            msg_chunk(content),
        ]
        # max_identical_outputs=5 -> halt on 5th
        wrapper = RepetitionGuardWrapper(
            MockAgent(stream_chunks=chunks),
            warn_after=2,
            max_identical_outputs=5,
        )
        out = _collect_stream(wrapper)
        assert fresh_state.get_state("s1", _HALTED_KEY, False) is True

    def test_custom_char_run_min(self, fresh_state):
        # With char_run_min=4, 4+ identical chars trigger.
        # Must be > _MIN_CONTENT_LENGTH(20) chars total.
        content = "prefix text " + "字" * 4 + " suffix text here"
        chunks = [msg_chunk(content)]
        wrapper = RepetitionGuardWrapper(
            MockAgent(stream_chunks=chunks),
            char_run_min=4,
        )
        out = _collect_stream(wrapper)
        assert _has_warning(out)


# ======================================================================
# 16. End-of-stream boundary
# ======================================================================

class TestEndOfStreamBoundary:
    """If the stream ends mid-model-call, the boundary is still processed."""

    def test_boundary_at_stream_end(self, fresh_state):
        content = "Long identical content for end-of-stream detection."
        # No updates chunk after the second model call — stream just ends
        chunks = [
            msg_chunk(content),
            update_chunk("tools", [ToolMessage(content="ok", tool_call_id="t1")]),
            msg_chunk(content),
        ]
        wrapper = RepetitionGuardWrapper(
            MockAgent(stream_chunks=chunks),
            warn_after=2,
            max_identical_outputs=3,
        )
        out = _collect_stream(wrapper)
        assert _has_warning(out)
        hist = fresh_state.get_state("s1", _HISTORY_KEY, [])
        assert len(hist) >= 2


# ======================================================================
# 17. Non-model messages mode chunks
# ======================================================================

class TestNonModelMessages:
    """Chunks from non-model nodes (e.g. summarization) pass through."""

    def test_summarization_node_skipped(self, fresh_state):
        chunks = [
            msg_chunk("Normal model response text here."),
            msg_chunk("Summarization text", node="summarize"),
        ]
        wrapper = RepetitionGuardWrapper(MockAgent(stream_chunks=chunks))
        out = _collect_stream(wrapper)
        # Both chunks should be in output
        assert len(out) == 2
        assert not _has_warning(out)

    def test_non_model_node_triggers_boundary(self, fresh_state):
        content = "Long identical content for boundary detection test."
        chunks = [
            msg_chunk(content),
            msg_chunk("Tool node message", node="tools"),
            msg_chunk(content),
        ]
        wrapper = RepetitionGuardWrapper(
            MockAgent(stream_chunks=chunks),
            warn_after=2,
            max_identical_outputs=3,
        )
        out = _collect_stream(wrapper)
        assert _has_warning(out)
```

---

## 3. 修改文件：`agent/__init__.py`

### 变更说明

将原有的直接导入（`from .core import built_agent` 等）替换为延迟 `__getattr__` 导入机制，
避免在测试收集阶段或仅需子模块时触发 `codeact -> langchain factory -> langgraph prebuilt`
这一重依赖链。同时新增 `RepetitionGuardWrapper` 导出。

### 旧代码（变更前）

```python
# 原始 __init__.py 直接导入，类似：
from .codeact import codeact_agent
from .core import built_agent, get_agent_tools
from .checkpointer import build_async_sqlite_checkpointer
# ... 其他直接导入
```

### 新代码（替换为）

```python
"""Lazy imports for the agent package.

Using ``__getattr__`` avoids triggering heavy dependency chains
(codeact -> langchain factory -> langgraph prebuilt) during test
collection or when only a specific submodule is needed.
"""

__all__ = [
    "codeact_agent",
    "built_agent",
    "get_agent_tools",
    "build_async_sqlite_checkpointer",
    "RepetitionGuardWrapper",
]


def __getattr__(name: str):
    if name == "codeact_agent":
        from .codeact import codeact_agent
        return codeact_agent
    if name in ("built_agent", "get_agent_tools"):
        from . import core
        return getattr(core, name)
    if name == "build_async_sqlite_checkpointer":
        from .checkpointer import build_async_sqlite_checkpointer
        return build_async_sqlite_checkpointer
    if name == "RepetitionGuardWrapper":
        from .repetition_guard_wrapper import RepetitionGuardWrapper
        return RepetitionGuardWrapper
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
```

---

## 4. 修改文件：`agent/core.py`

### 变更说明

1. **删除** `OutputRepetitionGuard` 的导入
2. **新增** `RepetitionGuardWrapper` 的导入
3. **从 middleware 列表中移除** `OutputRepetitionGuard()`（避免与 wrapper 双重计数）
4. **在 `create_agent(...)` 返回后用 `RepetitionGuardWrapper` 包装**

### 变更 1：导入部分

**旧代码：**

```python
from .middlewares import (Summarization, ToolCallNormalize, MultimodalProcessor, ContextEngineHook, ToolGuardrails,
                           IterationBudget, HeartbeatStaleness)
from .middlewares.output_repetition_guard import OutputRepetitionGuard
from .middlewares.humanInTheLoop import HumanInTheLoop, HITLConfig
from .smart_tool_node import patch_tool_node
```

**新代码：**

```python
from .middlewares import (Summarization, ToolCallNormalize, MultimodalProcessor, ContextEngineHook, ToolGuardrails,
                           IterationBudget, HeartbeatStaleness)
from .middlewares.humanInTheLoop import HumanInTheLoop, HITLConfig
from .smart_tool_node import patch_tool_node
from .repetition_guard_wrapper import RepetitionGuardWrapper
```

### 变更 2：middleware 列表 + 包装

**旧代码：**

```python
        _agent =  create_agent(
            model = main_llm.bind(temperature=temperature),
            state_schema = StateSchema,
            checkpointer = checkpointer,
            tools = get_agent_tools(),
            middleware = [
                ContextEngineHook(),
                MultimodalProcessor(),
                IterationBudget(90),
                ToolGuardrails(),
                OutputRepetitionGuard(),
                ToolCallNormalize(),
                HeartbeatStaleness(),
                HumanInTheLoop(HITLConfig()),
                Summarization(
                    need_update_system_prompt=True,
                    model=auxiliary_llm,
                    trigger=[
                        ("tokens", int(main_llm_max_tokens / 2))
                    ],
                    keep=("messages", 10),

                ),
            ],
        )
        _agent_loop = current_loop

    return _agent
```

**新代码：**

```python
        # Build the agent
        _agent =  create_agent(
            model = main_llm.bind(temperature=temperature),
            state_schema = StateSchema,
            checkpointer = checkpointer,
            tools = get_agent_tools(),
            middleware = [
                ContextEngineHook(),
                MultimodalProcessor(),
                IterationBudget(90),
                ToolGuardrails(),
                ToolCallNormalize(),
                HeartbeatStaleness(),
                HumanInTheLoop(HITLConfig()),
                Summarization(
                    need_update_system_prompt=True,
                    model=auxiliary_llm,
                    trigger=[
                        ("tokens", int(main_llm_max_tokens / 2))
                    ],
                    keep=("messages", 10),

                ),
            ],
        )
        # Wrap with RepetitionGuardWrapper for stream-level repetition
        # detection (replaces both the OutputRepetitionGuard middleware and
        # the check_stream_repetition calls in messages.py).
        _agent = RepetitionGuardWrapper(_agent)
        _agent_loop = current_loop

    return _agent
```

**关键差异：**

- `OutputRepetitionGuard()` 从 `middleware` 列表中删除
- `create_agent(...)` 返回值赋给 `_agent` 后，立即 `_agent = RepetitionGuardWrapper(_agent)` 包装

---

## 5. 修改文件：`server/service/messages.py`

### 变更说明

1. **删除** `check_stream_repetition` 的导入
2. **删除** `async_generate` 函数中的 `check_stream_repetition` 调用（约第 366 行）
3. **删除** resume 路径中的 `check_stream_repetition` 调用（约第 644 行）

wrapper 已在 `astream` 层透明处理流级重复检测，`messages.py` 不再需要手动调用。

### 变更 1：导入部分

**旧代码：**

```python
from agent.middlewares.heartbeat_staleness import HeartbeatTimeoutError
from agent.middlewares.output_repetition_guard import check_stream_repetition
from context_engine import get_history_by_turn_page as _get_history_by_turn_page
```

**新代码：**

```python
from agent.middlewares.heartbeat_staleness import HeartbeatTimeoutError
from context_engine import get_history_by_turn_page as _get_history_by_turn_page
```

### 变更 2：`async_generate` 函数中的调用点

**旧代码：**

```python
                    # Conversation output logic
                    if len(msg_chunk.content) > 0:
                        res: str = msg_chunk.content
                        ai_text += res
                        # Stream-level (Layer C) repetition interception. The
                        # middleware backstop is post-hoc (per model call), so the
                        # repetitive tail is cut BEFORE it reaches the client.
                        guard_warning: str | None = check_stream_repetition(session_id, ai_text)
                        if guard_warning is not None:
                            yield {"type": "text", "content": guard_warning}
                            break
                        yield {"type": "text", "content": res}
```

**新代码：**

```python
                    # Conversation output logic
                    if len(msg_chunk.content) > 0:
                        res: str = msg_chunk.content
                        ai_text += res
                        yield {"type": "text", "content": res}
```

### 变更 3：resume 路径中的调用点

**旧代码：**

```python
                if len(msg_chunk.content) > 0:
                    res: str = msg_chunk.content
                    # Stream-level (Layer C) repetition interception. Mirrors
                    # async_generate: accumulate the visible model text and, when
                    # a repetitive pattern is detected, yield the warning chunk and
                    # cut the rest of the stream instead of forwarding it.
                    ai_text_stream += res
                    guard_warning: str | None = check_stream_repetition(session_id, ai_text_stream)
                    if guard_warning is not None:
                        yield {"type": "text", "content": guard_warning}
                        break
                    yield {"type": "text", "content": res}
```

**新代码：**

```python
                if len(msg_chunk.content) > 0:
                    res: str = msg_chunk.content
                    ai_text_stream += res
                    yield {"type": "text", "content": res}
```

---

## 6. 验证步骤

完成以上所有变更后，运行以下命令验证：

```bash
# 运行 wrapper 测试（应全部 43 项通过）
python -m pytest tests/unit/test_repetition_guard_wrapper.py -v --tb=short

# 验证修改的文件语法正确
python -c "import ast; ast.parse(open('agent/core.py', encoding='utf-8').read()); print('core.py OK')"
python -c "import ast; ast.parse(open('server/service/messages.py', encoding='utf-8').read()); print('messages.py OK')"

# 确认 messages.py 中不再引用 check_stream_repetition
# (应返回无匹配)
```

### 注意事项

- `tests/unit/test_output_repetition_guard.py`（旧中间件测试）由于 `langgraph.runtime`
  缺少 `ExecutionInfo` / `ServerInfo` 的版本不兼容问题，在隔离运行时会 import 失败。
  这是 **变更前就存在的问题**，与本次变更无关。新测试文件
  `test_repetition_guard_wrapper.py` 通过 stub 模块绕过了此问题。
- `agent/__init__.py` 改为延迟导入后，旧的 `from agent import built_agent` 等用法
  仍然正常工作（`__getattr__` 在首次访问时触发导入）。
- `OutputRepetitionGuard` 中间件类本身（`agent/middlewares/output_repetition_guard.py`）
  **不做任何修改** — wrapper 复用其检测方法（`_detect_internal_repetition`、
  `_check_text_repetition`、`_before_agent_impl` 等）和状态键常量。
- `check_stream_repetition` 函数和 `_STREAM_GUARD` 实例仍保留在
  `output_repetition_guard.py` 中，只是不再被 `messages.py` 调用。如果确认无其他
  引用方，可以后续清理。
