"""The stream layer moves inline reasoning to the reasoning channel (B4).

The unit contract lives in ``tests/agent/security/test_think_scrub.py``; this file
covers the half a module-only implementation would miss — that ``StreamTurn``
actually calls it in the streaming path a provider chunk really takes, that the
answer text in frames and in ``turn.ai_text`` (the persisted answer) is the
scrubbed one, and that the recovered chain-of-thought comes back on the
``reasoning`` frames the client renders as its thinking block. Dropping the CoT
would pass a leak test and still be a functional regression, so both halves are
asserted.
"""

import asyncio
from typing import Any

import pytest
from langchain_core.messages import AIMessageChunk

from pub.types.message import MultiModalMessage
from runtime import state_register_mem
from server.service import messages as m

pytestmark = [pytest.mark.unit, pytest.mark.timeout(60)]

SID = "sess-think-scrub-1"
_META = {"langgraph_node": "model"}


def _mc(chunk: AIMessageChunk) -> tuple[str, Any]:
    return ("messages", (chunk, dict(_META)))


def _turn(chunks_per_call) -> m._GenerateTurn:
    turn = m._GenerateTurn(SID, MultiModalMessage(text="q"), is_stream=True, origin=None)

    async def _noop_prepare():
        return None

    turn._prepare = _noop_prepare

    class _Agent:
        def __init__(self):
            self._per_call = list(chunks_per_call)

        def astream(self, *args, **kwargs):
            chunks = self._per_call.pop(0) if self._per_call else []

            async def _gen():
                for c in chunks:
                    yield c

            return _gen()

    turn._agent = _Agent()
    return turn


async def _collect(agen) -> list[dict]:
    return [f async for f in agen]


def _text_of(frames: list[dict]) -> str:
    return "".join(f["content"] for f in frames if f.get("type") == "text")


def _reasoning_of(frames: list[dict]) -> str:
    return "".join(f["content"] for f in frames if f.get("type") == "reasoning")


@pytest.fixture(autouse=True)
def _clean_session():
    yield
    state_register_mem.clear_session(SID)


def _stop() -> tuple[str, Any]:
    return _mc(AIMessageChunk(content="", response_metadata={"finish_reason": "stop"}))


def test_an_inline_reasoning_block_never_reaches_a_frame_or_ai_text():
    turn = _turn(
        [
            [
                _mc(AIMessageChunk(content="<think>")),
                _mc(AIMessageChunk(content="the API key is in /etc/shadow")),
                _mc(AIMessageChunk(content="</think>Here is the answer.")),
                _stop(),
            ]
        ]
    )

    frames = asyncio.run(_collect(turn.run()))

    assert "the API key" not in _text_of(frames)
    assert _text_of(frames) == "Here is the answer."
    assert turn.ai_text == "Here is the answer."


def test_the_recovered_reasoning_is_redirected_to_the_reasoning_channel():
    turn = _turn(
        [
            [
                _mc(AIMessageChunk(content="<think>")),
                _mc(AIMessageChunk(content="weighing options")),
                _mc(AIMessageChunk(content="</think>answer")),
                _stop(),
            ]
        ]
    )

    frames = asyncio.run(_collect(turn.run()))

    assert _reasoning_of(frames) == "weighing options"
    assert _text_of(frames) == "answer"
    assert turn.ai_text == "answer", "reasoning must not be persisted as the answer"
    assert turn._has_reasoning is True


def test_a_tag_split_across_chunks_is_still_scrubbed_and_recovered():
    """The provider chooses the boundaries; the scrubber must not care."""
    turn = _turn(
        [
            [
                _mc(AIMessageChunk(content="<thi")),
                _mc(AIMessageChunk(content="nk>hidden</thin")),
                _mc(AIMessageChunk(content="k>visible")),
                _stop(),
            ]
        ]
    )

    frames = asyncio.run(_collect(turn.run()))

    assert "hidden" not in _text_of(frames)
    assert _reasoning_of(frames) == "hidden"
    assert turn.ai_text == "visible"


def test_an_unterminated_block_is_redirected_and_the_held_fragment_returned():
    turn = _turn(
        [
            [
                _mc(AIMessageChunk(content="Answer.<think>never closes")),
                _stop(),
            ]
        ]
    )

    frames = asyncio.run(_collect(turn.run()))

    assert _text_of(frames) == "Answer."
    assert _reasoning_of(frames) == "never closes"
    assert turn.ai_text == "Answer."


def test_a_held_back_fragment_that_never_became_a_tag_is_released_at_the_end():
    turn = _turn(
        [
            [
                _mc(AIMessageChunk(content="a < b and <thi")),
                _stop(),
            ]
        ]
    )

    frames = asyncio.run(_collect(turn.run()))

    assert _text_of(frames) == "a < b and <thi"
    assert _reasoning_of(frames) == ""
    assert turn.ai_text == "a < b and <thi"


def test_plain_content_passes_through_unchanged():
    turn = _turn([[_mc(AIMessageChunk(content="just text")), _stop()]])

    frames = asyncio.run(_collect(turn.run()))

    assert _text_of(frames) == "just text"
    assert _reasoning_of(frames) == ""
    assert turn.ai_text == "just text"


def test_an_explicit_reasoning_delta_still_gets_its_own_frame():
    """The additional_kwargs path is untouched: it is the reasoning bubble's feed."""
    turn = _turn(
        [
            [
                _mc(AIMessageChunk(content="", additional_kwargs={"reasoning_content": "cot"})),
                _mc(AIMessageChunk(content="answer")),
                _stop(),
            ]
        ]
    )

    frames = asyncio.run(_collect(turn.run()))

    assert _reasoning_of(frames) == "cot"
    assert turn.ai_text == "answer"
