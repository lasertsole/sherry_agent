"""Unit tests for pub/func/message/overflow_clip.py (P1-2 tail clip).

Pins the pure clip contract:
- only the trailing CONTIGUOUS ToolMessage batch is eligible (first
  non-ToolMessage ends the scan);
- config gates: enabled / max_remove / min_keep;
- budget: clipping stops at the token target, the batch boundary, or
  max_remove — 0 means "take the maximum eligible batch";
- None when nothing worth clipping (caller degrades to the existing route);
- message identity (id / tool_call_id / name / additional_kwargs) survives;
- P0-2 eviction pointers and P2-4 read_file slice markers survive in the stub;
- repeated clipping does not destroy markers (idempotent);
- no orphan ToolMessage is injected and pairing stays sanitizer-clean.
"""

from __future__ import annotations

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from config.features import SUMMARIZATION, TOKEN_ESTIMATION
from pub.func.message.eviction import EVICTION_PREFIX, READ_FILE_SLICE_NOTICE
from pub.func.message.overflow_clip import CLIP_MARKER, clip_overflow_tail
from pub.func.transcript_repair import sanitize_tool_use_result_pairing

CHARS_PER_TOKEN = TOKEN_ESTIMATION["chars_per_token"]

pytestmark = [pytest.mark.unit]


def _big(tokens: int, call_id: str) -> ToolMessage:
    """ASCII ToolMessage whose content estimate is exactly ``tokens``.

    Every ``call_id`` here is shorter than CHARS_PER_TOKEN, so the id adds 0
    estimated tokens and the content carries all of them.
    """
    return ToolMessage(content="x" * (tokens * CHARS_PER_TOKEN), tool_call_id=call_id)


def _small(call_id: str = "s0", text: str = "ok") -> ToolMessage:
    return ToolMessage(content=text, tool_call_id=call_id)


def _stubbed(messages: list) -> list[int]:
    return [
        index
        for index, message in enumerate(messages)
        if isinstance(message, ToolMessage) and CLIP_MARKER in str(message.content)
    ]


def _padded(*messages) -> list:
    """Prefix padding so the list clears ``overflow_clip_min_keep``."""
    return [
        HumanMessage(content="pad0"),
        AIMessage(content="pad1"),
        HumanMessage(content="pad2"),
        AIMessage(content="pad3"),
        *messages,
    ]


class TestTailBatchIdentification:
    def test_clips_only_trailing_contiguous_tool_batch(self):
        messages = _padded(
            _big(2_000, "old"),
            AIMessage(content="done"),
            _big(2_000, "t1"),
            _big(2_000, "t2"),
        )
        result = clip_overflow_tail(messages, target_tokens=10_000)
        assert result is not None
        assert _stubbed(result) == [6, 7]
        # the older ToolMessage separated by the AIMessage is untouched
        assert result[4] is messages[4]

    def test_no_trailing_tool_message_returns_none(self):
        messages = _padded(
            _big(2_000, "t1"),
            AIMessage(content="tail is not a tool result"),
        )
        assert clip_overflow_tail(messages, target_tokens=10_000) is None

    def test_stops_at_non_tool_before_older_tools(self):
        messages = _padded(
            _big(2_000, "t0"),
            HumanMessage(content="q"),
            _big(2_000, "t1"),
        )
        result = clip_overflow_tail(messages, target_tokens=10_000)
        assert result is not None
        assert _stubbed(result) == [6]


class TestConfigGates:
    def test_disabled_returns_none(self, monkeypatch):
        monkeypatch.setitem(SUMMARIZATION, "overflow_clip_enabled", False)
        messages = [HumanMessage(content="q")] + [_big(2_000, f"t{i}") for i in range(4)]
        assert clip_overflow_tail(messages, target_tokens=1) is None

    def test_min_keep_floor_returns_none(self):
        messages = [
            HumanMessage(content="q"),
            AIMessage(content="a"),
            AIMessage(content="a2"),
            _big(2_000, "t1"),
            _big(2_000, "t2"),
        ]
        assert len(messages) <= 5  # == SUMMARIZATION["overflow_clip_min_keep"]
        assert clip_overflow_tail(messages, target_tokens=1) is None

    def test_one_past_min_keep_clips(self):
        messages = [*[AIMessage(content=f"a{i}") for i in range(4)], _big(2_000, "t1")]
        messages.insert(0, HumanMessage(content="q"))
        assert len(messages) == 6
        assert clip_overflow_tail(messages, target_tokens=1) is not None

    def test_max_remove_caps_single_pass(self, monkeypatch):
        monkeypatch.setitem(SUMMARIZATION, "overflow_clip_max_remove", 2)
        messages = [HumanMessage(content="q")] + [_big(2_000, f"t{i}") for i in range(5)]
        result = clip_overflow_tail(messages, target_tokens=0)
        assert result is not None
        assert _stubbed(result) == [4, 5]

    def test_zero_max_remove_returns_none(self, monkeypatch):
        monkeypatch.setitem(SUMMARIZATION, "overflow_clip_max_remove", 0)
        messages = [HumanMessage(content="q")] + [_big(2_000, f"t{i}") for i in range(4)]
        assert clip_overflow_tail(messages, target_tokens=1) is None


class TestBudget:
    def test_stops_once_target_tokens_freed(self):
        messages = _padded(*[_big(1_000, f"t{i}") for i in range(5)])
        result = clip_overflow_tail(messages, target_tokens=1)
        assert result is not None
        # the first stub already frees the token target -> exactly one clipped
        assert len(_stubbed(result)) == 1

    def test_target_beyond_batch_clips_whole_batch(self):
        messages = _padded(*[_big(1_000, f"t{i}") for i in range(4)])
        result = clip_overflow_tail(messages, target_tokens=99_000)
        assert result is not None
        assert len(_stubbed(result)) == 4

    def test_zero_target_takes_maximum_eligible_batch(self):
        messages = _padded(*[_big(1_000, f"t{i}") for i in range(4)])
        result = clip_overflow_tail(messages, target_tokens=0)
        assert result is not None
        assert len(_stubbed(result)) == 4


class TestNothingToClip:
    def test_small_tools_return_none(self):
        messages = [
            HumanMessage(content="q"),
            _small("t1"),
            _small("t2"),
            _small("t3"),
            _small("t4"),
            _small("t5"),
        ]
        assert clip_overflow_tail(messages, target_tokens=1) is None

    def test_mixed_small_skipped_older_big_clipped(self):
        messages = _padded(
            _big(2_000, "old"),
            _small("tiny"),
        )
        result = clip_overflow_tail(messages, target_tokens=1)
        assert result is not None
        assert _stubbed(result) == [4]
        assert result[5] is messages[5]


class TestStubContract:
    def test_identity_and_metadata_survive(self):
        tool = ToolMessage(
            content="x" * 8_000,
            tool_call_id="call-1",
            name="search",
            id="msg-1",
            additional_kwargs={"origin": "tool"},
        )
        messages = [
            HumanMessage(content="q"),
            AIMessage(content="a"),
            AIMessage(content="a2"),
            AIMessage(content="a3"),
            AIMessage(content="a4"),
            tool,
        ]
        result = clip_overflow_tail(messages, target_tokens=0)
        assert result is not None
        stub = result[-1]
        assert isinstance(stub, ToolMessage)
        assert stub.id == "msg-1"
        assert stub.tool_call_id == "call-1"
        assert stub.name == "search"
        assert stub.additional_kwargs == {"origin": "tool"}
        assert CLIP_MARKER in stub.content

    def test_input_is_not_mutated(self):
        tool = _big(2_000, "t1")
        messages = [
            HumanMessage(content="q"),
            AIMessage(content="a"),
            AIMessage(content="a2"),
            AIMessage(content="a3"),
            AIMessage(content="a4"),
            tool,
        ]
        before = tool.content
        result = clip_overflow_tail(messages, target_tokens=0)
        assert result is not messages
        assert tool.content == before
        assert len(result) == len(messages)

    def test_eviction_pointer_survives(self):
        evicted = ToolMessage(
            content=(
                "[evicted to: /sessions/s1/evicted/call-1_abcd1234.txt]\n"
                "--- head (5 lines) ---\n" + "x" * 8_000
            ),
            tool_call_id="call-1",
        )
        messages = [
            HumanMessage(content="q"),
            AIMessage(content="a"),
            AIMessage(content="a2"),
            AIMessage(content="a3"),
            AIMessage(content="a4"),
            evicted,
        ]
        result = clip_overflow_tail(messages, target_tokens=0)
        assert result is not None
        stub_text = result[-1].content
        assert EVICTION_PREFIX in stub_text
        assert "/sessions/s1/evicted/call-1_abcd1234.txt" in stub_text
        assert "read_file" in stub_text

    def test_read_file_slice_marker_survives(self):
        sliced = ToolMessage(
            content=("x" * 8_000) + READ_FILE_SLICE_NOTICE,
            tool_call_id="call-1",
        )
        messages = [
            HumanMessage(content="q"),
            AIMessage(content="a"),
            AIMessage(content="a2"),
            AIMessage(content="a3"),
            AIMessage(content="a4"),
            sliced,
        ]
        result = clip_overflow_tail(messages, target_tokens=0)
        assert result is not None
        assert READ_FILE_SLICE_NOTICE.strip() in result[-1].content

    def test_non_text_blocks_survive(self):
        multimodal = ToolMessage(
            content=[
                {"type": "text", "text": "x" * 8_000},
                {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}},
            ],
            tool_call_id="call-1",
        )
        base = [
            HumanMessage(content="q"),
            AIMessage(content="a"),
            AIMessage(content="a2"),
            AIMessage(content="a3"),
            AIMessage(content="a4"),
        ]
        result = clip_overflow_tail([*base, multimodal], target_tokens=0)
        assert result is not None
        blocks = result[-1].content
        assert isinstance(blocks, list)
        assert blocks[0]["type"] == "text"
        assert CLIP_MARKER in blocks[0]["text"]
        assert blocks[1]["type"] == "image_url"

    def test_repeated_clip_is_noop_and_markers_survive(self):
        evicted = ToolMessage(
            content=("[evicted to: /sessions/s1/evicted/call-1_abcd1234.txt]\n" + "y" * 8_000),
            tool_call_id="call-1",
        )
        messages = [
            HumanMessage(content="q"),
            AIMessage(content="a"),
            AIMessage(content="a2"),
            AIMessage(content="a3"),
            AIMessage(content="a4"),
            evicted,
        ]
        first = clip_overflow_tail(messages, target_tokens=0)
        assert first is not None
        second = clip_overflow_tail(first, target_tokens=0)
        assert second is None
        assert EVICTION_PREFIX in first[-1].content
        assert "/sessions/s1/evicted/call-1_abcd1234.txt" in first[-1].content


class TestPairingInvariant:
    def test_sanitizer_accepts_clipped_list_unchanged(self):
        ai = AIMessage(
            content="",
            tool_calls=[
                {"name": "search", "args": {"q": "x"}, "id": "c1"},
                {"name": "search", "args": {"q": "y"}, "id": "c2"},
            ],
        )
        messages = [
            HumanMessage(content="q1"),
            AIMessage(content="a1"),
            HumanMessage(content="q2"),
            AIMessage(content="a2"),
            ai,
            _big(1_000, "c1"),
            _big(1_000, "c2"),
        ]
        clipped = clip_overflow_tail(messages, target_tokens=0)
        assert clipped is not None
        sanitized = sanitize_tool_use_result_pairing(clipped)
        assert sanitized is clipped
        tool_ids = [message.tool_call_id for message in clipped if isinstance(message, ToolMessage)]
        assert tool_ids == ["c1", "c2"]

    def test_no_orphan_tool_message_injected(self):
        messages = [
            HumanMessage(content="q"),
            AIMessage(content="a"),
            AIMessage(content="a2"),
            AIMessage(content="a3"),
            AIMessage(content="a4"),
            _big(1_000, "c1"),
        ]
        clipped = clip_overflow_tail(messages, target_tokens=0)
        assert clipped is not None
        assert len(clipped) == len(messages)
        assert all(getattr(message, "tool_call_id", "") != "_overflow_clip" for message in clipped)
