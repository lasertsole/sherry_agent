"""Stream-layer content-filter detection tests (error-handling plan, module C).

Covers the two detection points that flag ``llm_content_filter_blocked`` for
the LLMRetryMiddleware: the explicit ``finish_reason == "content_filter"``
branch in ``_should_text_continue`` and the mid-stream keyword heuristic for
providers that cut the stream without a content_filter finish_reason.
"""

import asyncio
from typing import Any

import pytest
from langchain_core.messages import AIMessageChunk

from runtime import state_register_mem
from server.service import messages as m
from pub.types.message import MultiModalMessage

pytestmark = [pytest.mark.unit, pytest.mark.timeout(60)]

SID = "sess-cfilter-1"
_META = {"langgraph_node": "model"}


def _mc(chunk: AIMessageChunk) -> tuple[str, Any]:
    return ("messages", (chunk, dict(_META)))


def _source(chunks):
    async def _gen():
        for c in chunks:
            yield c

    return _gen()


class _CountingStreamAgent:
    def __init__(self, chunks_per_call):
        self._per_call = list(chunks_per_call)
        self.calls = 0

    def astream(self, *args, **kwargs):
        self.calls += 1
        chunks = self._per_call.pop(0) if self._per_call else []
        return _source(chunks)


def _turn(chunks_per_call) -> tuple[m._GenerateTurn, _CountingStreamAgent]:
    turn = m._GenerateTurn(
        SID,
        MultiModalMessage(text="q"),
        is_stream=True,
        origin=None,
    )

    async def _noop_prepare():
        return None

    turn._prepare = _noop_prepare
    agent = _CountingStreamAgent(chunks_per_call)
    turn._agent = agent
    return turn, agent


async def _collect(agen) -> list[dict]:
    return [f async for f in agen]


@pytest.fixture(autouse=True)
def _clean_session():
    yield
    state_register_mem.clear_session(SID)


class TestFinishReasonContentFilter:
    def test_sets_flag_and_skips_continuation(self):
        chunks = [
            _mc(AIMessageChunk(content="partial answer")),
            _mc(
                AIMessageChunk(
                    content="",
                    response_metadata={"finish_reason": "content_filter"},
                )
            ),
        ]
        turn, agent = _turn([chunks])
        asyncio.run(_collect(turn.run()))
        assert state_register_mem.get_state(SID, "llm_content_filter_blocked", False) is True
        assert agent.calls == 1

    def test_meta_frame_reports_content_filter(self):
        chunks = [
            _mc(AIMessageChunk(content="x")),
            _mc(
                AIMessageChunk(
                    content="",
                    response_metadata={"finish_reason": "content_filter"},
                )
            ),
        ]
        turn, _ = _turn([chunks])
        frames = asyncio.run(_collect(turn.run()))
        meta = [f for f in frames if f.get("type") == "meta"]
        assert meta and meta[0]["finish_reason"] == "content_filter"


class TestMidStreamDetection:
    def test_stream_cut_without_finish_reason_sets_flag(self):
        chunks = [
            _mc(AIMessageChunk(content="the answer starts fine, then new_sensitive")),
        ]
        turn, agent = _turn([chunks])
        asyncio.run(_collect(turn.run()))
        assert state_register_mem.get_state(SID, "llm_content_filter_blocked", False) is True
        assert turn.meta_finish_reason == "content_filter"
        assert agent.calls == 1

    def test_normal_stop_with_no_keywords_sets_no_flag(self):
        chunks = [
            _mc(AIMessageChunk(content="a perfectly normal answer")),
            _mc(AIMessageChunk(content="", response_metadata={"finish_reason": "stop"})),
        ]
        turn, _ = _turn([chunks])
        asyncio.run(_collect(turn.run()))
        assert state_register_mem.has_key(SID, "llm_content_filter_blocked") is False
        assert turn.meta_finish_reason == "stop"

    def test_length_truncation_still_continues(self):
        first = [
            _mc(AIMessageChunk(content="truncated answer")),
            _mc(AIMessageChunk(content="", response_metadata={"finish_reason": "length"})),
        ]
        second = [
            _mc(AIMessageChunk(content=" continued")),
            _mc(AIMessageChunk(content="", response_metadata={"finish_reason": "stop"})),
        ]
        turn, agent = _turn([first, second])
        frames = asyncio.run(_collect(turn.run()))
        texts = "".join(f["content"] for f in frames if f.get("type") == "text")
        assert "truncated answer continued" in texts
        assert agent.calls == 2
        assert state_register_mem.has_key(SID, "llm_content_filter_blocked") is False


class TestMidStreamDetectionOnError:
    def test_exception_with_filter_keyword_sets_flag_and_reraises(self):
        chunks = [_mc(AIMessageChunk(content="started, then content_filter hit"))]

        async def _failing_gen():
            for c in chunks:
                yield c
            raise ConnectionError("peer closed connection")

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
        assert state_register_mem.get_state(SID, "llm_content_filter_blocked", False) is True
        assert turn.meta_finish_reason == "content_filter"
