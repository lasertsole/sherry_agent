"""Module I — ContextLimitGuardWrapper tests.

Covers both defenses at stream level: the model-call boundary
force-compress (real ``usage_metadata`` crossing the compression threshold
arms Summarization's ``_FORCE_RECOVERY_KEY``) and the mid-stream output
budget (text chunks stop being forwarded once the estimated output tokens
exceed the budget, with a one-time marker), plus passthrough guarantees.
"""

import asyncio
from typing import Any

import pytest
from langchain_core.messages import AIMessageChunk

from agent.context_limit_guard_wrapper import ContextLimitGuardWrapper
from runtime import state_register_mem

pytestmark = [pytest.mark.unit, pytest.mark.timeout(60)]

SID = "sess-ctxguard-1"
_META = {"langgraph_node": "model"}


def _text_chunk(content: str) -> tuple[str, Any]:
    return ("messages", (AIMessageChunk(content=content), dict(_META)))


def _usage_chunk(input_tokens: int, output_tokens: int) -> tuple[str, Any]:
    return (
        "messages",
        (
            AIMessageChunk(
                content="",
                usage_metadata={
                    "input_tokens": input_tokens,
                    "output_tokens": output_tokens,
                    "total_tokens": input_tokens + output_tokens,
                },
            ),
            dict(_META),
        ),
    )


def _tool_chunk() -> tuple[str, Any]:
    return (
        "messages",
        (
            AIMessageChunk(
                content="",
                tool_call_chunks=[
                    {
                        "name": "bash",
                        "args": "{}",
                        "id": "t1",
                        "index": 0,
                        "type": "tool_call_chunk",
                    }
                ],
            ),
            dict(_META),
        ),
    )


def _updates_chunk(node: str = "tools") -> tuple[str, Any]:
    return ("updates", {node: {"messages": []}})


class _FakeGraph:
    """Scripted inner graph: replays chunk lists per astream call."""

    def __init__(self, chunks):
        self._chunks = chunks
        self.ainvoke_called = 0
        self.astream_args: list[dict] = []

    async def _gen(self, chunks):
        for c in chunks:
            yield c

    def astream(self, *args, **kwargs):
        self.astream_args.append(kwargs)
        return self._gen(self._chunks)

    async def ainvoke(self, *args, **kwargs):
        self.ainvoke_called += 1
        return "invoke-result"


def _wrapper(chunks, window: int, check_interval: int = 1):
    graph = _FakeGraph(chunks)
    wrapper = ContextLimitGuardWrapper(graph, context_window=window, check_interval=check_interval)
    return wrapper, graph


async def _collect(agen) -> list[tuple]:
    return [c async for c in agen]


def _run(chunks, window: int, check_interval: int = 1):
    wrapper, graph = _wrapper(chunks, window, check_interval)
    frames = asyncio.run(
        _collect(wrapper.astream(input={"session_id": SID}, stream_mode=["messages", "updates"]))
    )
    return frames, graph


@pytest.fixture(autouse=True)
def _clean_session():
    yield
    state_register_mem.clear_session(SID)


class TestForceCompress:
    def test_no_usage_keeps_flag_unset(self):
        frames, _ = _run(
            [_updates_chunk("model"), _updates_chunk("tools")],
            window=1000,  # threshold = 800, but no usage chunk was captured
        )
        assert state_register_mem.get_state(SID, "summarization_force_recovery", False) is False
        assert len(frames) == 2

    def test_real_input_tokens_over_threshold_set_flag(self):
        _run(
            [
                _usage_chunk(input_tokens=850, output_tokens=0),
                _updates_chunk("tools"),
            ],
            window=1000,
        )
        assert state_register_mem.get_state(SID, "summarization_force_recovery", False) is True

    def test_predictive_input_plus_output_sets_flag(self):
        _run(
            [
                _usage_chunk(input_tokens=520, output_tokens=400),
                _updates_chunk("tools"),
            ],
            window=1000,  # threshold = 800; 520 < 800 but 920 >= 800
        )
        assert state_register_mem.get_state(SID, "summarization_force_recovery", False) is True

    def test_below_threshold_keeps_flag_unset(self):
        _run(
            [
                _usage_chunk(input_tokens=100, output_tokens=50),
                _updates_chunk("tools"),
            ],
            window=1000,
        )
        assert state_register_mem.has_key(SID, "summarization_force_recovery") is False

    def test_stream_end_flushes_final_usage(self):
        # No updates chunk at all: the post-loop flush must still check.
        _run([_usage_chunk(input_tokens=950, output_tokens=0)], window=1000)
        assert state_register_mem.get_state(SID, "summarization_force_recovery", False) is True

    def test_flag_shared_with_summarization_contract(self):
        from agent.middlewares.summarization import _FORCE_RECOVERY_KEY as MW_KEY

        _run(
            [_usage_chunk(input_tokens=950, output_tokens=0), _updates_chunk("tools")],
            window=1000,
        )
        assert state_register_mem.get_state(SID, MW_KEY, False) is True


class TestMidStreamOutputBudget:
    def test_text_stops_being_forwarded_after_budget(self):
        # window=1000 → budget = 200 tokens = 800 chars; check every chunk.
        big = "x" * 300
        chunks = [
            _text_chunk(big),
            _text_chunk(big),
            _text_chunk(big),
            _text_chunk(big),
        ]
        frames, _ = _run(chunks, window=1000, check_interval=1)

        text_chunks = [f for f in frames if f[0] == "messages"]
        # window=1000 → 200-token budget = 800 chars. Checks run per chunk:
        # 300→75 tok, 600→150 tok (forwarded), 900→225 tok > 200 → the cut
        # fires and the triggering chunk is suppressed too, then chunk 4 is
        # suppressed. Exactly one marker appears where the cut happened.
        contents = [str(f[1][0].content) for f in text_chunks]
        marker = [c for c in contents if "truncated" in c]
        assert len(marker) == 1
        assert sum(1 for c in contents if c == big) == 2

    def test_tool_call_chunks_still_forwarded_after_cut(self):
        big = "y" * 900  # 225 est. tokens > 200 budget
        chunks = [
            _text_chunk(big),
            _tool_chunk(),
            _text_chunk("more text"),
        ]
        frames, _ = _run(chunks, window=1000, check_interval=1)

        msgs = [f[1][0] for f in frames if f[0] == "messages"]
        tool_chunks = [m for m in msgs if getattr(m, "tool_call_chunks", None)]
        assert len(tool_chunks) == 1
        assert not any(str(m.content) == "more text" for m in msgs)

    def test_updates_boundary_resets_the_budget(self):
        big = "z" * 900
        chunks = [
            _text_chunk(big),  # call 1 exceeds the budget → cut
            _updates_chunk("tools"),  # boundary → reset
            _text_chunk("fresh call text"),  # call 2 forwards normally
        ]
        frames, _ = _run(chunks, window=1000, check_interval=1)

        contents = [str(f[1][0].content) for f in frames if f[0] == "messages"]
        assert "fresh call text" in contents

    def test_small_output_untouched(self):
        chunks = [_text_chunk("short answer"), _text_chunk(" still short")]
        frames, _ = _run(chunks, window=100000, check_interval=1)

        msgs = [f[1][0] for f in frames if f[0] == "messages"]
        assert len(msgs) == 2


class TestPassthrough:
    def test_usage_chunks_pass_through(self):
        frames, _ = _run([_usage_chunk(input_tokens=10, output_tokens=5)], window=1000)
        assert len(frames) == 1
        assert frames[0][1][0].usage_metadata["input_tokens"] == 10

    def test_ainvoke_delegates(self):
        wrapper, graph = _wrapper([], window=1000)
        result = asyncio.run(wrapper.ainvoke(input={"session_id": SID}))
        assert result == "invoke-result"
        assert graph.ainvoke_called == 1

    def test_unknown_attributes_delegate_to_inner(self):
        wrapper, graph = _wrapper([], window=1000)
        assert wrapper.inner is graph
        # arbitrary attribute access falls through to the inner graph
        assert graph.ainvoke_called == 0


class TestSessionExtraction:
    def test_missing_session_id_raises(self):
        wrapper, _ = _wrapper([], window=1000)
        with pytest.raises(RuntimeError, match="session_id"):
            asyncio.run(_collect(wrapper.astream(input={}, stream_mode=["messages"])))
