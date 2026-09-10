"""H.2 reasoning-only continuation tests.

Covers the reasoning-only truncation scenario: a thinking model spent the
whole output budget on reasoning with no visible answer — the turn
re-streams with the dedicated ``_REASONING_ONLY_PROMPT`` (max 2 retries)
instead of the standard text-continuation prompt, while every other
truncation scenario keeps its existing behavior.
"""

import asyncio
from typing import Any

import pytest
from langchain_core.messages import AIMessageChunk

from runtime import state_register_mem
from server.service import messages as m
from type.message import MultiModalMessage

pytestmark = [pytest.mark.unit, pytest.mark.timeout(60)]

SID = "sess-reasononly-1"
_META = {"langgraph_node": "model"}


def _mc(chunk: AIMessageChunk) -> tuple[str, Any]:
    return ("messages", (chunk, dict(_META)))


def _source(chunks):
    async def _gen():
        for c in chunks:
            yield c

    return _gen()


class _RecordingStreamAgent:
    """Serves a fresh chunk list per astream call and records the input."""

    def __init__(self, chunks_per_call):
        self._per_call = list(chunks_per_call)
        self.inputs: list[dict] = []
        self.calls = 0

    def astream(self, *args, **kwargs):
        self.calls += 1
        self.inputs.append(kwargs.get("input") or (args[0] if args else None))
        chunks = self._per_call.pop(0) if self._per_call else []
        return _source(chunks)


def _turn(chunks_per_call) -> tuple[m._GenerateTurn, _RecordingStreamAgent]:
    turn = m._GenerateTurn(SID, MultiModalMessage(text="q"), is_stream=True, origin=None)

    async def _noop_prepare():
        return None

    turn._prepare = _noop_prepare
    agent = _RecordingStreamAgent(chunks_per_call)
    turn._agent = agent
    return turn, agent


async def _collect(agen) -> list[dict]:
    return [f async for f in agen]


@pytest.fixture(autouse=True)
def _clean_session():
    yield
    state_register_mem.clear_session(SID)


class TestDecisionMatrix:
    def _decision_turn(self) -> m._GenerateTurn:
        turn = _turn([])[0]
        return turn

    def test_reasoning_only_truncation_is_reasoning_only(self):
        turn = self._decision_turn()
        turn.meta_finish_reason = "length"
        turn._has_reasoning = True
        turn._has_visible_text = False
        assert turn._should_text_continue() == (True, True)

    def test_text_truncation_is_standard_continuation(self):
        turn = self._decision_turn()
        turn.meta_finish_reason = "length"
        turn._has_reasoning = True
        turn._has_visible_text = True
        assert turn._should_text_continue() == (True, False)

    def test_no_output_falls_back_to_text_continuation(self):
        turn = self._decision_turn()
        turn.meta_finish_reason = "length"
        turn._has_reasoning = False
        turn._has_visible_text = False
        assert turn._should_text_continue() == (True, False)

    def test_tool_call_truncation_never_continues(self):
        turn = self._decision_turn()
        turn.meta_finish_reason = "length"
        turn._has_reasoning = True
        turn._has_tool_calls = True
        assert turn._should_text_continue() == (False, False)

    def test_reasoning_only_cap_two_then_stop(self):
        turn = self._decision_turn()
        turn.meta_finish_reason = "length"
        turn._has_reasoning = True
        turn._reasoning_only_retries = m._MAX_REASONING_ONLY_RETRIES
        assert turn._should_text_continue() == (False, False)

    def test_content_filter_branch_preserved(self):
        turn = self._decision_turn()
        turn.meta_finish_reason = "content_filter"
        assert turn._should_text_continue() == (False, False)
        assert state_register_mem.get_state(SID, "llm_content_filter_blocked", False) is True


class TestReasoningOnlyContinuationFlow:
    def test_reasoning_only_stream_reprompts_with_dedicated_prompt(self):
        first = [
            _mc(AIMessageChunk(content="", additional_kwargs={"reasoning_content": "thinking"})),
            _mc(AIMessageChunk(content="", response_metadata={"finish_reason": "length"})),
        ]
        second = [
            _mc(AIMessageChunk(content="final answer")),
            _mc(AIMessageChunk(content="", response_metadata={"finish_reason": "stop"})),
        ]
        turn, agent = _turn([first, second])
        frames = asyncio.run(_collect(turn.run()))

        assert agent.calls == 2
        sent = agent.inputs[1]["messages"][0].content
        assert sent == m._REASONING_ONLY_PROMPT
        assert sent != m._CONTINUATION_PROMPT
        texts = "".join(f["content"] for f in frames if f.get("type") == "text")
        assert "final answer" in texts

    def test_reasoning_only_cap_stops_the_loop(self):
        thinking_round = [
            _mc(AIMessageChunk(content="", additional_kwargs={"reasoning_content": "thinking"})),
            _mc(AIMessageChunk(content="", response_metadata={"finish_reason": "length"})),
        ]
        turn, agent = _turn([thinking_round] * 5)
        asyncio.run(_collect(turn.run()))

        # initial stream + 2 reasoning-only retries, then the cap stops it
        assert agent.calls == 3
        assert turn._reasoning_only_retries == 2

    def test_text_truncation_still_uses_standard_prompt(self):
        first = [
            _mc(AIMessageChunk(content="half answer")),
            _mc(AIMessageChunk(content="", response_metadata={"finish_reason": "length"})),
        ]
        second = [
            _mc(AIMessageChunk(content=" continued")),
            _mc(AIMessageChunk(content="", response_metadata={"finish_reason": "stop"})),
        ]
        turn, agent = _turn([first, second])
        asyncio.run(_collect(turn.run()))

        assert agent.calls == 2
        sent = agent.inputs[1]["messages"][0].content
        assert sent == m._CONTINUATION_PROMPT
