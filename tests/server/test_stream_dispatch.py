"""TDD tests for audit 2.1.1 — shared stream-dispatch engine
(server/service/stream_dispatch.py + the _GenerateTurn/_ResumeTurn wiring in
server/service/messages.py).

Behavior-pins the ``stream_mode=["messages", "updates"]`` dispatch loop that
``async_generate`` / ``resume_agent`` previously duplicated: updates-mode tool
handling, messages-mode tool-call tracking + streamed-args accumulation,
text/reasoning emission, the cancel/timeout/error paths, and the per-turn
differences (generate's meta chunk + tool-call note + interrupt markers +
generator close; resume's HITL-denial tool_result path).
"""

import asyncio
from typing import Any

import pytest
from agent.middlewares.heartbeat_staleness import HeartbeatTimeoutError
from langchain_core.messages import AIMessageChunk, ToolMessage
from langgraph.types import Command

from pub.types.message import MultiModalMessage
from runtime import state_register_mem
from server.service import messages as m
from server.service.stream_dispatch import (
    StreamTurn,
    _accumulate_pending_args,
    _clear_pending_args,
    _get_pending_args,
)

pytestmark = [pytest.mark.unit, pytest.mark.timeout(60)]

SID = "sess-engine-1"

_MODEL_META = {"langgraph_node": "model"}


def _model_chunk(**kwargs) -> AIMessageChunk:
    return AIMessageChunk(**kwargs)


def _mc(chunk: AIMessageChunk) -> tuple[str, Any]:
    return ("messages", (chunk, dict(_MODEL_META)))


def _updates(msgs: list) -> tuple[str, Any]:
    return ("updates", {"tools": {"messages": msgs}})


def _tm(content: str = "done", tool_call_id: str = "t1") -> ToolMessage:
    return ToolMessage(content=content, tool_call_id=tool_call_id)


def _source(chunks):
    async def _gen():
        for c in chunks:
            yield c

    return _gen()


def _make_stream(agen):
    async def _create():
        return "stream", agen

    return _create


async def _collect(agen) -> list[dict]:
    return [f async for f in agen]


class _PlainTurn(StreamTurn):
    """Engine test double fed a synthetic chunk list."""

    def __init__(self, chunks):
        super().__init__(SID)
        self._chunks = chunks

    async def _create_source(self):
        return "stream", _source(self._chunks)


class _FakeAgent:
    """Agent double for _GenerateTurn._prepare (built_agent is never the SUT)."""

    def astream(self, *args, **kwargs):
        async def _empty():
            return
            yield

        return _empty()

    async def ainvoke(self, *args, **kwargs):
        return {"messages": []}


@pytest.fixture(autouse=True)
def _fake_built_agent(monkeypatch):
    async def _build(*args, **kwargs):
        return _FakeAgent()

    monkeypatch.setattr(m, "built_agent", _build)


@pytest.fixture(autouse=True)
def _reset_turn_state():
    yield
    state_register_mem.set_state(SID, "answering", False)
    state_register_mem.set_state(SID, "current_tool_name", "")
    state_register_mem.set_state(SID, "current_tool_id", "")
    state_register_mem.delete_state(SID, "is_stream_turn")
    _clear_pending_args(SID)


class TestUpdatesMode:
    def test_tool_message_emits_result_then_end_and_resets_tool_id(self):
        state_register_mem.set_state(SID, "current_tool_name", "bash")
        state_register_mem.set_state(SID, "current_tool_id", "t1")
        _accumulate_pending_args(SID, "t1", {"cmd": "ls"})

        frames = asyncio.run(_collect(_PlainTurn([_updates([_tm("file list", "t1")])]).run()))

        assert frames == [
            {
                "type": "tool_result",
                "content": "file list",
                "tool_id": "t1",
                "tool_name": "bash",
                "args": {"cmd": "ls"},
                "error": False,
            },
            {"type": "tool_end", "content": "bash"},
        ]
        assert state_register_mem.get_state(SID, "current_tool_id", "") == ""
        assert _get_pending_args(SID, "t1") == {}  # consumed from the stash

    def test_error_status_surfaced(self):
        tm = _tm("boom", "t9")
        tm.status = "error"

        frames = asyncio.run(_collect(_PlainTurn([_updates([tm])]).run()))

        assert frames[0]["error"] is True

    def test_non_tool_updates_skipped(self):
        frames = asyncio.run(_collect(_PlainTurn([_updates(["not-a-msg"])]).run()))
        assert frames == []


class TestMessagesMode:
    def test_text_and_reasoning_forwarded_and_accumulated(self):
        chunk = _model_chunk(content="你好")
        chunk.additional_kwargs = {"reasoning_content": "thinking-1"}

        turn = _PlainTurn([_mc(chunk)])
        frames = asyncio.run(_collect(turn.run()))

        assert {"type": "text", "content": "你好"} in frames
        assert {"type": "reasoning", "content": "thinking-1"} in frames
        assert turn.ai_text == "你好"

    def test_non_model_metadata_skipped(self):
        turn = _PlainTurn(
            [("messages", (_model_chunk(content="ignored"), {"langgraph_node": "tools"}))]
        )

        frames = asyncio.run(_collect(turn.run()))

        assert frames == []

    def test_summarization_source_skipped(self):
        turn = _PlainTurn(
            [
                (
                    "messages",
                    (
                        _model_chunk(content="summary text"),
                        {"langgraph_node": "model", "lc_source": "summarization"},
                    ),
                )
            ]
        )

        frames = asyncio.run(_collect(turn.run()))

        assert frames == []

    def test_tool_start_fires_once_and_tool_result_carries_accumulated_args(self):
        turn = _PlainTurn(
            [
                _mc(
                    _model_chunk(
                        content="",
                        tool_call_chunks=[{"name": "bash", "args": "", "id": "t1", "index": 0}],
                    )
                ),
                _mc(
                    _model_chunk(
                        content="",
                        tool_call_chunks=[{"name": "", "args": '{"cmd":', "id": None, "index": 0}],
                    )
                ),
                _mc(
                    _model_chunk(
                        content="",
                        tool_call_chunks=[{"name": "", "args": ' "ls"}', "id": None, "index": 0}],
                    )
                ),
                _updates([_tm("done", "t1")]),
            ]
        )

        frames = asyncio.run(_collect(turn.run()))

        starts = [f for f in frames if f["type"] == "tool_start"]
        assert len(starts) == 1  # repeat_flag suppresses duplicate tool starts
        assert starts[0]["content"] == "bash"
        assert starts[0]["args"] == {}  # tool_start fires on the first (empty-args) chunk
        # the streamed fragments accumulated into the stash by tool_result time
        results = [f for f in frames if f["type"] == "tool_result"]
        assert results == [
            {
                "type": "tool_result",
                "content": "done",
                "tool_id": "t1",
                "tool_name": "bash",
                "args": {"cmd": "ls"},
                "error": False,
            }
        ]

    def test_model_metadata_captured_from_chunks(self):
        turn = _PlainTurn(
            [
                _mc(
                    _model_chunk(
                        content="",
                        response_metadata={"model_name": "glm-5"},
                        usage_metadata={"input_tokens": 5, "output_tokens": 7, "total_tokens": 12},
                    )
                )
            ]
        )

        asyncio.run(_collect(turn.run()))

        assert turn.meta_model_name == "glm-5"
        assert turn.meta_input_tokens == 5
        assert turn.meta_output_tokens == 7


class TestLifecycle:
    def test_answering_false_mid_stream_yields_request_cancelled(self):
        """run() sets answering=True at turn start (as the original did); the
        answering-False check catches a stop requested DURING the stream."""
        turn = _PlainTurn([_mc(_model_chunk(content="x")), _mc(_model_chunk(content="y"))])

        async def _scenario():
            collected: list[dict] = []
            async for frame in turn.run():
                collected.append(frame)
                state_register_mem.set_state(SID, "answering", False)
            return collected

        frames = asyncio.run(_scenario())

        assert frames[0] == {"type": "text", "content": "x"}
        assert frames[-1] == {"type": "text", "content": "Request cancelled"}
        assert state_register_mem.get_state(SID, "answering") is False

    def test_generate_cancel_writes_marker_then_frame(self, monkeypatch):
        markers: list[tuple[str, str]] = []

        async def _marker(session_id, partial_text, reason):
            markers.append((partial_text, reason))

        monkeypatch.setattr(m, "_write_interrupt_marker", _marker)

        turn = m._GenerateTurn(SID, MultiModalMessage(text="q"), is_stream=True, origin=None)
        turn._create_source = _make_stream(
            _source([_mc(_model_chunk(content="partial")), _mc(_model_chunk(content="more"))])
        )

        async def _scenario() -> list[dict]:
            collected: list[dict] = []
            async for frame in turn.run():
                collected.append(frame)
                # flip answering after the first chunk → the second chunk's
                # answering check raises CancelledError inside the engine
                state_register_mem.set_state(SID, "answering", False)
            return collected

        frames = asyncio.run(_scenario())

        assert frames[0] == {"type": "text", "content": "partial"}
        assert frames[-1] == {"type": "text", "content": "Request cancelled"}
        assert markers == [("partial", "cancelled")]

    def test_heartbeat_timeout_yields_timeout_frame(self):
        async def _raise():
            raise HeartbeatTimeoutError("idle")
            yield  # pragma: no cover — makes this an async generator

        turn = _PlainTurn([])
        turn._create_source = _make_stream(_raise())

        frames = asyncio.run(_collect(turn.run()))

        assert frames == [
            {
                "type": "text",
                "content": "\n\n**[Heartbeat Timeout]** Agent idle timeout exceeded — automatically terminated.",
            }
        ]

    def test_generic_exception_reraises_after_cleanup(self):
        async def _boom():
            raise ValueError("model exploded")
            yield  # pragma: no cover

        turn = _PlainTurn([])
        turn._create_source = _make_stream(_boom())

        with pytest.raises(ValueError, match="model exploded"):
            asyncio.run(_collect(turn.run()))

        # finally cleanup still ran: state reset
        assert state_register_mem.get_state(SID, "answering") is False
        assert state_register_mem.get_state(SID, "current_tool_name", "") == ""


class TestGenerateTurnWiring:
    def test_final_meta_frame_with_defaults(self):
        turn = m._GenerateTurn(SID, MultiModalMessage(text="q"), is_stream=True, origin=None)
        turn._create_source = _make_stream(_source([]))

        frames = asyncio.run(_collect(turn.run()))

        assert frames[-1] == {
            "type": "meta",
            "content": "",
            "model_name": "",
            "input_tokens": 0,
            "output_tokens": 0,
            "reasoning_tokens": 0,
            "finish_reason": "",
        }

    def test_meta_carries_captured_model_and_usage(self):
        turn = m._GenerateTurn(SID, MultiModalMessage(text="q"), is_stream=True, origin=None)
        turn._create_source = _make_stream(
            _source(
                [
                    _mc(
                        _model_chunk(
                            content="hi",
                            response_metadata={"model_name": "glm-5"},
                            usage_metadata={
                                "input_tokens": 3,
                                "output_tokens": 4,
                                "total_tokens": 7,
                            },
                        )
                    )
                ]
            )
        )

        frames = asyncio.run(_collect(turn.run()))

        assert frames[-1]["model_name"] == "glm-5"
        assert frames[-1]["input_tokens"] == 3
        assert frames[-1]["output_tokens"] == 4

    def test_tool_start_appends_transcript_note(self):
        turn = m._GenerateTurn(SID, MultiModalMessage(text="q"), is_stream=True, origin=None)
        turn._note_tool_start("bash")

        assert turn.ai_text == "\n\n**Calling tool bash...**"

    def test_resume_turn_has_no_transcript_note(self):
        turn = m._ResumeTurn(SID, "approve")
        turn._note_tool_start("bash")

        assert turn.ai_text == ""

    def test_stream_source_closed_on_cleanup(self):
        class _CloseSpy:
            def __init__(self):
                self.closed = False

            async def aclose(self):
                self.closed = True

        spy = _CloseSpy()
        turn = m._GenerateTurn(SID, MultiModalMessage(text="q"), is_stream=True, origin=None)

        asyncio.run(turn._cleanup("stream", spy))

        assert spy.closed is True

    def test_invoke_source_emits_text_then_meta(self):
        class _LastMsg:
            content = "final answer"
            response_metadata = {"model_name": "ds-v3"}
            usage_metadata = {"input_tokens": 9, "output_tokens": 2, "total_tokens": 11}

        result = {"messages": [_LastMsg()]}

        class _InvokeAgent:
            async def ainvoke(self, *args, **kwargs):
                return result

        turn = m._GenerateTurn(SID, MultiModalMessage(text="q"), is_stream=False, origin=None)

        # run() calls _prepare() which rebuilds the agent — skip it and inject
        # the scripted agent directly (the built_agent path is not the SUT here).
        async def _noop_prepare() -> None:
            return None

        turn._prepare = _noop_prepare
        turn._agent = _InvokeAgent()
        frames = asyncio.run(_collect(turn.run()))

        assert frames[0] == {"type": "text", "content": "final answer"}
        assert frames[-1] == {
            "type": "meta",
            "content": "",
            "model_name": "ds-v3",
            "input_tokens": 9,
            "output_tokens": 2,
            "reasoning_tokens": 0,
            "finish_reason": "",
        }
        assert turn.ai_text == "final answer"


class TestResumeTurnWiring:
    def test_hitl_denial_tool_message_emits_result_with_null_args(self):
        turn = m._ResumeTurn(SID, "reject")
        tm = _tm("rejected by user", "t1")
        metadata = {"langgraph_node": "HumanInTheLoop.after_model"}

        frames = turn._extra_messages_frames(tm, metadata)

        assert frames == [
            {
                "type": "tool_result",
                "content": "rejected by user",
                "tool_id": "t1",
                "tool_name": "",
                "args": None,  # null (not {}) — client keeps its existing args
                "error": False,
            }
        ]

    def test_normal_messages_chunks_not_consumed_by_denial_path(self):
        turn = m._ResumeTurn(SID, "approve")
        tm = _tm("ok", "t1")

        assert turn._extra_messages_frames(tm, {"langgraph_node": "model"}) == []
        assert (
            turn._extra_messages_frames(
                _model_chunk(content="x"), {"langgraph_node": "HumanInTheLoop.after_model"}
            )
            == []
        )

    def test_resume_yields_no_meta_chunk(self):
        turn = m._ResumeTurn(SID, "approve")

        assert turn._final_frames() == []

    def test_resume_uses_command_resume_with_session_id_injected(self, monkeypatch):
        captured: dict[str, Any] = {}

        class _FakeAgent:
            def astream(self, input, config=None, stream_mode=None, **kwargs):
                captured["input"] = input
                captured["config"] = config
                captured["stream_mode"] = stream_mode
                return _source([])

        async def _fake_built_agent(force_rebuild=False):
            return _FakeAgent()

        monkeypatch.setattr(m, "built_agent", _fake_built_agent)

        turn = m._ResumeTurn(SID, "edit", "fix it", {"cmd": "new"})
        asyncio.run(turn._prepare())
        _, agen = asyncio.run(turn._create_source())
        asyncio.run(_collect(agen))

        assert isinstance(captured["input"], Command)
        assert captured["input"].resume == {
            "session_id": SID,
            "decisions": [
                {"type": "edit", "message": "fix it", "edited_action": {"args": {"cmd": "new"}}}
            ],
        }
        assert captured["stream_mode"] == ["messages", "updates"]

    def test_resume_cleanup_does_not_close_source(self):
        class _CloseSpy:
            def __init__(self):
                self.closed = False

            async def aclose(self):
                self.closed = True

        spy = _CloseSpy()
        turn = m._ResumeTurn(SID, "approve")

        asyncio.run(turn._cleanup("stream", spy))

        assert spy.closed is False  # resume historically never closes

    def test_resume_end_to_end_frames_through_wrapper(self, monkeypatch):
        """resume_agent(...) yields the same frames the engine produced."""

        async def _fake_built_agent(force_rebuild=False):
            class _FakeAgent:
                def astream(self, input, config=None, stream_mode=None, **kwargs):
                    return _source([_mc(_model_chunk(content="resumed"))])

            return _FakeAgent()

        monkeypatch.setattr(m, "built_agent", _fake_built_agent)

        frames = asyncio.run(_collect(m.resume_agent(SID, "approve")))

        assert frames == [{"type": "text", "content": "resumed"}]
