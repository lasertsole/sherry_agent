"""Partial-stream-stub tests — stream-layer flagging + middleware retry.

Covers the network-cut vs output-truncation distinction: StreamTurn flags a
stream that died mid-output (``llm_partial_stream_stub``) and
LLMRetryMiddleware converts the flag into a classified retry on the next
model call, so MaxTokensBoost never sees a network-cut "truncation".
"""

import asyncio
from typing import Any

import pytest
from langchain_core.messages import AIMessageChunk

from runtime import state_register_mem
from server.service import messages as m
from pub.types.message import MultiModalMessage

pytestmark = [pytest.mark.unit, pytest.mark.timeout(60)]

SID = "sess-stubstream-1"
_META = {"langgraph_node": "model"}


def _mc(chunk: AIMessageChunk) -> tuple[str, Any]:
    return ("messages", (chunk, dict(_META)))


def _source(chunks):
    async def _gen():
        for c in chunks:
            yield c

    return _gen()


def _turn(chunks_per_call) -> tuple[m._GenerateTurn, Any]:
    turn = m._GenerateTurn(SID, MultiModalMessage(text="q"), is_stream=True, origin=None)

    async def _noop_prepare():
        return None

    turn._prepare = _noop_prepare

    class _Agent:
        def __init__(self):
            self._per_call = list(chunks_per_call)

        def astream(self, *args, **kwargs):
            chunks = self._per_call.pop(0) if self._per_call else []
            return _source(chunks)

    turn._agent = _Agent()
    return turn, turn._agent


async def _collect(agen) -> list[dict]:
    return [f async for f in agen]


@pytest.fixture(autouse=True)
def _clean_session():
    yield
    state_register_mem.clear_session(SID)


class TestStreamSideStubFlag:
    def test_exception_mid_stream_with_text_sets_stub_and_cause(self):
        async def _failing_gen():
            yield _mc(AIMessageChunk(content="partial answer"))
            raise TimeoutError("request timed out")

        class _RaisingAgent:
            def astream(self, *args, **kwargs):
                return _failing_gen()

        turn = m._GenerateTurn(SID, MultiModalMessage(text="q"), is_stream=True, origin=None)

        async def _noop_prepare():
            return None

        turn._prepare = _noop_prepare
        turn._agent = _RaisingAgent()
        with pytest.raises(TimeoutError):
            asyncio.run(_collect(turn.run()))

        assert state_register_mem.get_state(SID, "llm_partial_stream_stub", False) is True
        assert state_register_mem.get_state(SID, "llm_partial_stream_cause", "") == "timeout"

    def test_silent_end_without_finish_reason_sets_stub_timeout(self):
        turn, _ = _turn([[_mc(AIMessageChunk(content="cut answer"))]])
        asyncio.run(_collect(turn.run()))

        assert state_register_mem.get_state(SID, "llm_partial_stream_stub", False) is True
        assert state_register_mem.get_state(SID, "llm_partial_stream_cause", "") == "timeout"

    def test_reasoning_only_silent_end_sets_stub(self):
        turn, _ = _turn(
            [[_mc(AIMessageChunk(content="", additional_kwargs={"reasoning_content": "thinking"}))]]
        )
        asyncio.run(_collect(turn.run()))

        assert state_register_mem.get_state(SID, "llm_partial_stream_stub", False) is True

    def test_normal_stop_sets_no_stub(self):
        turn, _ = _turn(
            [
                [
                    _mc(AIMessageChunk(content="done")),
                    _mc(AIMessageChunk(content="", response_metadata={"finish_reason": "stop"})),
                ]
            ]
        )
        asyncio.run(_collect(turn.run()))

        assert state_register_mem.has_key(SID, "llm_partial_stream_stub") is False

    def test_exception_without_output_sets_no_stub(self):
        async def _failing_gen():
            raise ConnectionError("connection reset")
            yield  # pragma: no cover

        class _RaisingAgent:
            def astream(self, *args, **kwargs):
                return _failing_gen()

        turn = m._GenerateTurn(SID, MultiModalMessage(text="q"), is_stream=True, origin=None)

        async def _noop_prepare():
            return None

        turn._prepare = _noop_prepare
        turn._agent = _RaisingAgent()
        with pytest.raises(ConnectionError):
            asyncio.run(_collect(turn.run()))

        assert state_register_mem.has_key(SID, "llm_partial_stream_stub") is False

    def test_content_filter_cut_sets_no_stub(self):
        turn, _ = _turn(
            [[_mc(AIMessageChunk(content="the answer starts fine, then new_sensitive"))]]
        )
        asyncio.run(_collect(turn.run()))

        assert state_register_mem.has_key(SID, "llm_partial_stream_stub") is False
        assert state_register_mem.get_state(SID, "llm_content_filter_blocked", False) is True
        assert state_register_mem.get_state(SID, "llm_content_filter_terminated", False) is True

    def test_exception_with_explicit_filter_finish_reason_sets_flags(self):
        async def _failing_gen():
            yield _mc(AIMessageChunk(content="started"))
            yield _mc(
                AIMessageChunk(content="", response_metadata={"finish_reason": "content_filter"})
            )
            raise ConnectionError("connection reset")
            yield  # pragma: no cover

        class _RaisingAgent:
            def astream(self, *args, **kwargs):
                return _failing_gen()

        turn = m._GenerateTurn(SID, MultiModalMessage(text="q"), is_stream=True, origin=None)

        async def _noop_prepare():
            return None

        turn._prepare = _noop_prepare
        turn._agent = _RaisingAgent()
        with pytest.raises(ConnectionError):
            asyncio.run(_collect(turn.run()))

        assert state_register_mem.has_key(SID, "llm_partial_stream_stub") is False
        assert state_register_mem.get_state(SID, "llm_content_filter_terminated", False) is True
        assert state_register_mem.get_state(SID, "llm_content_filter_blocked", False) is True


def _tm(content: str, tool_call_id: str):
    from langchain_core.messages import ToolMessage

    return ToolMessage(content=content, tool_call_id=tool_call_id)


class TestDroppedToolNames:
    def _tool_chunk(self, name="write_file", tool_id="t1") -> AIMessageChunk:
        return AIMessageChunk(
            content="",
            tool_call_chunks=[
                {"name": name, "args": "", "id": tool_id, "index": 0, "type": "tool_call_chunk"}
            ],
        )

    def _raising_turn(self, chunks) -> m._GenerateTurn:
        async def _failing_gen():
            for c in chunks:
                yield c
            raise ConnectionError("connection reset")
            yield  # pragma: no cover

        class _RaisingAgent:
            def astream(self, *args, **kwargs):
                return _failing_gen()

        turn = m._GenerateTurn(SID, MultiModalMessage(text="q"), is_stream=True, origin=None)

        async def _noop_prepare():
            return None

        turn._prepare = _noop_prepare
        turn._agent = _RaisingAgent()
        return turn

    def test_stream_death_mid_tool_call_surfaces_name_in_error(self):
        turn = self._raising_turn(
            [
                _mc(AIMessageChunk(content="working on it")),
                _mc(self._tool_chunk(name="write_file", tool_id="t1")),
            ]
        )
        with pytest.raises(ConnectionError, match="write_file"):
            asyncio.run(_collect(turn.run()))

    def test_completed_tool_call_not_reported_as_dropped(self):
        updates = ("updates", {"tools": {"messages": [_tm("ok", "t1")]}})
        turn = self._raising_turn(
            [
                _mc(self._tool_chunk(name="bash", tool_id="t1")),
                updates,
                _mc(AIMessageChunk(content="now writing")),
                _mc(self._tool_chunk(name="write_file", tool_id="t2")),
            ]
        )
        with pytest.raises(ConnectionError) as excinfo:
            asyncio.run(_collect(turn.run()))

        assert "write_file" in str(excinfo.value)
        assert "bash" not in str(excinfo.value)

    def test_no_tool_calls_no_warning(self):
        turn = self._raising_turn([_mc(AIMessageChunk(content="plain text"))])
        with pytest.raises(ConnectionError) as excinfo:
            asyncio.run(_collect(turn.run()))

        assert "tool-call" not in str(excinfo.value)
