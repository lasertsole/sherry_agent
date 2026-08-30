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
from unittest.mock import MagicMock

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
_mw_stub.AgentMiddleware = type(
    "AgentMiddleware",
    (),
    {
        "__init__": lambda self, *a, **kw: None,
    },
)
_mw_stub.AgentState = dict
_mw_stub.SummarizationMiddleware = type(
    "SummarizationMiddleware",
    (),
    {
        "__init__": lambda self, *a, **kw: None,
    },
)
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
from langchain_core.messages import AIMessage, AIMessageChunk, ToolMessage

from agent.middlewares.output_repetition_guard import (
    _HISTORY_KEY,
    _INTERNAL_WARNED_KEY,
    _HALTED_KEY,
    _REASONING_HISTORY_KEY,
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
        _collect_stream(wrapper)
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

        assert (
            fresh_state.get_state("s1", _HISTORY_KEY, "X") == []
            or len(fresh_state.get_state("s1", _HISTORY_KEY, [])) <= 1
        )
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
            return await wrapper.ainvoke(input={"session_id": "s1", "messages": []}, config={})

        asyncio.run(_run())
        assert fresh_state.get_state("s1", _HALTED_KEY, False) is False

    def test_reset_allows_rearming(self, fresh_state):
        """After per-turn reset, a fresh repetitive stream warns again."""
        repetitive = "字" * 40
        wrapper = RepetitionGuardWrapper(MockAgent(stream_chunks=[msg_chunk(repetitive)]))
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
            return await wrapper.ainvoke(input={"session_id": "s1", "messages": []}, config={})

        out = asyncio.run(_run())
        last = out["messages"][-1]
        assert isinstance(last, AIMessage)
        assert "[Output Repetition Guard]" in last.content

    def test_clean_content_passes_through(self, fresh_state):
        clean = "This is a normal and varied response about multiple topics."
        result = {"messages": [AIMessage(content=clean)], "session_id": "s1"}
        wrapper = RepetitionGuardWrapper(MockAgent(invoke_result=result))

        async def _run():
            return await wrapper.ainvoke(input={"session_id": "s1", "messages": []}, config={})

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
            return await wrapper.ainvoke(input={"session_id": "s1", "messages": []}, config={})

        out = asyncio.run(_run())
        # Should pass through unchanged (tool_calls -> no detection)
        assert out["messages"][-1] is msg

    def test_internal_repetition_in_ainvoke_warns(self, fresh_state):
        """ainvoke detects internal repetition (char-run) in the result."""
        repetitive = "字" * 40
        result = {"messages": [AIMessage(content=repetitive)], "session_id": "s1"}
        wrapper = RepetitionGuardWrapper(MockAgent(invoke_result=result))

        async def _run():
            return await wrapper.ainvoke(input={"session_id": "s1", "messages": []}, config={})

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
            c
            for c in out
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
        _collect_stream(wrapper)
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
        _collect_stream(wrapper, session_id="sB")
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

        cmd = Command(
            resume={
                "session_id": "cmd-session",
                "decisions": [{"type": "approve"}],
            }
        )
        chunks = [msg_chunk("Normal clean response text here.")]
        wrapper = RepetitionGuardWrapper(MockAgent(stream_chunks=chunks))

        async def _run():
            out = []
            async for chunk in wrapper.astream(cmd, config={}, stream_mode=["messages", "updates"]):
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
        _collect_stream(wrapper)
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


# ======================================================================
# 18. Phantom-stream guard
# ======================================================================


class TestPhantomStreamGuard:
    """Model text arriving before ANY "updates" chunk cannot be live
    output of the middleware-equipped graph on a fresh dict-input run.
    With ``phantom_stream_guard=True`` it is dropped (loudly logged)
    instead of tripping the repetition cut that historically suppressed
    the REAL reply behind it (user saw only the 145-char warning)."""

    def test_flag_off_phantom_forwarded(self, fresh_state):
        """Default (off): legacy behavior — phantom text passes through."""
        text = "Phantom model text before any updates chunk."
        chunks = [msg_chunk(text)]
        wrapper = RepetitionGuardWrapper(MockAgent(stream_chunks=chunks))
        out = _collect_stream(wrapper)
        assert _text_parts(out) == text
        assert not _has_warning(out)

    def test_flag_off_repetitive_phantom_still_cuts(self, fresh_state):
        """Legacy failure-mode proof: without the guard, a repetitive
        phantom trips the cut and yields the warning (the historical bug)."""
        chunks = [msg_chunk("x" * 30)]
        wrapper = RepetitionGuardWrapper(MockAgent(stream_chunks=chunks))
        out = _collect_stream(wrapper)
        assert _has_warning(out)

    def test_phantom_dropped(self, fresh_state):
        """Guard ON: pre-update model text is dropped, not forwarded, and
        never touches the guard's detection state."""
        phantom = "Garbage model text arriving before any update!!"
        chunks = [msg_chunk(phantom)]
        wrapper = RepetitionGuardWrapper(MockAgent(stream_chunks=chunks), phantom_stream_guard=True)
        out = _collect_stream(wrapper)
        assert _text_parts(out) == ""
        assert not _has_warning(out)
        # phantom never reached detection state
        hist = fresh_state.get_state("s1", _HISTORY_KEY, [])
        assert hist == []

    def test_multiple_phantom_chunks_dropped(self, fresh_state):
        chunks = [
            msg_chunk("First phantom chunk before updates!!"),
            msg_chunk("Second phantom chunk before updates!"),
        ]
        wrapper = RepetitionGuardWrapper(MockAgent(stream_chunks=chunks), phantom_stream_guard=True)
        out = _collect_stream(wrapper)
        assert _text_parts(out) == ""

    def test_guard_on_repetitive_phantom_no_warning(self, fresh_state):
        """With the guard, the same repetitive phantom yields NO warning —
        it is dropped before accumulation/detection ever sees it."""
        chunks = [msg_chunk("x" * 30)]
        wrapper = RepetitionGuardWrapper(MockAgent(stream_chunks=chunks), phantom_stream_guard=True)
        out = _collect_stream(wrapper)
        assert not _has_warning(out)
        assert _text_parts(out) == ""

    def test_real_reply_survives_after_phantom(self, fresh_state):
        """THE regression case: a phantom must NOT arm call_cut; the real
        reply that follows updates must reach the client."""
        reply = "A perfectly normal and varied reply about many topics."
        chunks = [
            msg_chunk("x" * 30),  # repetitive phantom (would trip detection)
            update_chunk("MultimodalProcessor.before_agent"),
            msg_chunk(reply),
            update_chunk("model", [AIMessage(content=reply)]),
        ]
        wrapper = RepetitionGuardWrapper(MockAgent(stream_chunks=chunks), phantom_stream_guard=True)
        out = _collect_stream(wrapper)
        assert "x" * 30 not in _text_parts(out)  # phantom dropped
        assert reply in _text_parts(out)  # real reply survives
        assert not _has_warning(out)  # no false 145-char warning

    def test_updates_first_normal_flow(self, fresh_state):
        """Guard ON + healthy updates-first stream: text flows normally."""
        reply = "Normal varied answer once the graph has started."
        chunks = [
            update_chunk("MultimodalProcessor.before_agent"),
            msg_chunk(reply),
        ]
        wrapper = RepetitionGuardWrapper(MockAgent(stream_chunks=chunks), phantom_stream_guard=True)
        out = _collect_stream(wrapper)
        assert reply in _text_parts(out)
        assert not _has_warning(out)

    def test_command_resume_exempt(self, fresh_state):
        """Command(resume) streams may legitimately start with messages
        (the interrupted node re-executes) — the guard must NOT drop
        them."""
        from langgraph.types import Command

        cmd = Command(resume={"session_id": "cmd-session"})
        text = "Resume answer text that legitimately precedes updates."
        chunks = [msg_chunk(text)]
        wrapper = RepetitionGuardWrapper(MockAgent(stream_chunks=chunks), phantom_stream_guard=True)

        async def _run():
            out = []
            async for chunk in wrapper.astream(cmd, config={}, stream_mode=["messages", "updates"]):
                out.append(chunk)
            return out

        out = asyncio.run(_run())
        assert _text_parts(out) == text

    def test_empty_content_phantom_forwarded(self, fresh_state):
        """Only text-bearing pre-update chunks are dropped; empty model
        chunks (role-only) pass through harmlessly."""
        chunks = [
            msg_chunk(""),
            update_chunk("MultimodalProcessor.before_agent"),
            msg_chunk("Normal text after updates arrives just fine."),
        ]
        wrapper = RepetitionGuardWrapper(MockAgent(stream_chunks=chunks), phantom_stream_guard=True)
        out = _collect_stream(wrapper)
        assert "Normal text after updates arrives just fine." in _text_parts(out)
        # the empty chunk was forwarded as-is
        raw_msgs = [d for m, d in out if m == "messages"]
        assert any(
            isinstance(d, (tuple, list))
            and len(d) >= 2
            and isinstance(d[0], AIMessageChunk)
            and d[0].content == ""
            for d in raw_msgs
        )
