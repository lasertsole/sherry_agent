"""Integration test — RepetitionGuardWrapper through the live WS chat path.

Validates the browser-chat guard end-to-end WITHOUT a real LLM:

    WS client -- text --> async_generate --> _get_generator
        --> built_agent(force_rebuild=True)   [monkeypatched]
        --> RepetitionGuardWrapper( MockAgent(scripted repetitive chunks) )
        --> wrapper cuts repetition + yields [Output Repetition Guard] marker
        --> async_generate emits {"type": "text", "content": "[Output Repetition Guard]..."}
        --> WS sends the frame to the client

Since a real LLM won't naturally emit long repeated sentences, we drive the
wrapper through the exact server path with a controllable stub (the real
`server.service.messages.async_generate` plus a real `RepetitionGuardWrapper`
around a scripted repetitive inner agent). This proves the guard's marker
surfaces on the same WS frame the browser would render.
"""

import asyncio

import pytest

from unittest.mock import patch

from langchain_core.messages import AIMessageChunk

from agent.repetition_guard_wrapper import RepetitionGuardWrapper
from type.message import MultiModalMessage
from server.service import messages as _messages_mod


class MockInnerAgent:
    """Controllable stand-in for the compiled LangGraph graph.

    Yields a scripted sequence of ``(mode, data)`` stream tuples identical
    to what a real ``astream`` with ``stream_mode=["messages", "updates"]``
    would produce for a model emitting repeated text.
    """

    def __init__(self, stream_chunks: list):
        self._stream_chunks = stream_chunks

    async def astream(self, *args, **kwargs):
        for chunk in self._stream_chunks:
            yield chunk

    async def ainvoke(self, *args, **kwargs):
        # Not used in the streaming path; kept for interface completeness.
        raise NotImplementedError("ainvoke should not be called in this test")

    async def aget_state(self, config=None, **kwargs):
        class _State:
            values = {}

        return _State()

    async def aupdate_state(self, config=None, values=None, **kwargs):
        return None


def _msg_chunk(content: str, node: str = "model") -> tuple:
    """Build a ("messages", (AIMessageChunk, metadata)) chunk."""
    chunk = AIMessageChunk(content=content)
    metadata = {"langgraph_node": node}
    return ("messages", (chunk, metadata))


def _repetitive_script() -> list:
    """A model response that degenerates into a repeated phrase.

    Mirrors the phrase-repetition case proven in the unit tests
    ("我来帮你" repeated 8x -> phrase detector fires for > 5 repeats).

    The repetitive content is emitted as the FIRST content chunk so the
    wrapper's accumulated ``call_text`` stays a pure repeated phrase.  The
    phrase detector looks for a *contiguous* back-to-back run of a short
    phrase with no delimiter, so any non-repeating prefix (e.g. "好的，我来
    帮助你。") would break continuity and mask the pattern.  A real token
    stream typically emits the loop text first, then jams; this is the
    realistic degenerate case.

    *Chunk 1* accumulates 27 chars (>= _MIN_CONTENT_LENGTH=20) and trips
    ``_detect_internal_repetition`` mid-stream -> yield the guard marker and
    set ``call_cut=True``.
    *Chunk 2* is any further model text and MUST be suppressed (cut) before
    it reaches the client frame.
    """
    return [
        _msg_chunk("我来帮你" * 9),  # 27 chars of pure repeated phrase
        # After the guard cuts, further model text must be suppressed.
        _msg_chunk("这段不应该出现在客户端。"),
    ]


@pytest.fixture
def wrapper_disabled_state():
    """The wrapper's per-turn state uses runtime.state_register_mem.

    Use the real (in-memory) register so set_state/get_state work exactly
    as they would in production. Ensure a unique session per test to avoid
    cross-test leakage (the auto `clean_registers` fixture clears all
    sessions between tests).
    """
    yield


def _run_async_generate(session_id: str, text: str, mock_agent):
    """Drive the FULL server-side async_generate with a patched built_agent.

    Returns the list of emitted frames (just like the WS handler would
    serialize and forward to the browser).
    """
    message = MultiModalMessage(text=text)

    async def _drive():
        frames = []
        async for frame in _messages_mod.async_generate(session_id, message, is_stream=True):
            frames.append(frame)
        return frames

    with patch.object(_messages_mod, "built_agent", return_value=mock_agent):
        return asyncio.run(_drive())


def test_repetition_guard_surfaces_marker_in_live_path():
    """The guard's [Output Repetition Guard] marker reaches the WS frame."""
    wrapper = RepetitionGuardWrapper(MockInnerAgent(_repetitive_script()))
    frames = _run_async_generate("it-s1", "请继续你的分析。", wrapper)

    text_frames = [f["content"] for f in frames if f.get("type") == "text"]
    joined = "".join(text_frames)

    # The marker must reach the client as a text frame.
    assert "[Output Repetition Guard]" in joined, (
        "guard marker missing from text frames: %r" % text_frames
    )

    # The repetitive tail must be CUT (suppressed before reaching client).
    assert "我来帮你" not in joined
    assert "这段不应该出现在客户端" not in joined


def test_repetition_guard_emits_meta_end_frame():
    """Even when the guard cuts the stream, async_generate still emits a
    final `meta` frame (the WS client relies on it to finalize the turn)."""
    wrapper = RepetitionGuardWrapper(MockInnerAgent(_repetitive_script()))
    frames = _run_async_generate("it-s2", "继续说。", wrapper)

    end_frames = [f for f in frames if f.get("type") == "meta"]
    assert len(end_frames) == 1, "expected exactly one terminating meta frame"


def test_normal_text_never_triggers_guard():
    """Control: a non-repetitive stream passes straight through and does NOT
    emit the guard marker."""
    script = [
        _msg_chunk("这是一个"),
        _msg_chunk("完全正常的回答内容。"),
        _msg_chunk("没有重复的句子。"),
    ]
    wrapper = RepetitionGuardWrapper(MockInnerAgent(script))
    frames = _run_async_generate("it-s3", "你好。", wrapper)

    text_frames = [f["content"] for f in frames if f.get("type") == "text"]
    assert "[Output Repetition Guard]" not in "".join(text_frames)
