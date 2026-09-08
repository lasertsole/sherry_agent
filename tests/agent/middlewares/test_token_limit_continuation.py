"""Token-limit detection / text-continuation / max-tokens-boost tests.

Covers the three phases of the token-limit feature (plan doc retired; the
implementation lives in server/service/stream_dispatch.py,
server/service/messages.py and agent/middlewares/max_tokens_boost.py):
- Phase 1: finish_reason extraction into StreamTurn metadata + persistence
- Phase 2: text continuation re-stream loop in _GenerateTurn
- Phase 3: MaxTokensBoostMiddleware re-call with callback stripping
"""

import asyncio
from typing import Any

import pytest
from langchain_core.messages import AIMessage, AIMessageChunk

from agent.middlewares import max_tokens_boost as mtb
from agent.middlewares.max_tokens_boost import MaxTokensBoostMiddleware
from runtime import state_register_mem
from server.service import messages as m
from server.service.messages import _CONTINUATION_PROMPT, _MAX_CONTINUATION_RETRIES
from type.message import MultiModalMessage

pytestmark = [pytest.mark.unit, pytest.mark.timeout(60)]

SID = "sess-toklim-1"
_META = {"langgraph_node": "model"}


# ---- helpers ---------------------------------------------------------------


def _mc(chunk: AIMessageChunk) -> tuple[str, Any]:
    return ("messages", (chunk, dict(_META)))


def _source(chunks):
    async def _gen():
        for c in chunks:
            yield c

    return _gen()


def _chunked(chunk_dict: dict) -> AIMessageChunk:
    return AIMessageChunk(**chunk_dict)


def _tool_chunk(name="bash", tool_id="t1", args="{}") -> AIMessageChunk:
    return AIMessageChunk(
        content="",
        tool_call_chunks=[
            {"name": name, "args": args, "id": tool_id, "index": 0, "type": "tool_call_chunk"}
        ],
    )


class _FakeStreamAgent:
    """Serves a fresh chunk list per astream call (one per continuation round)."""

    def __init__(self, chunks_per_call):
        self._per_call = list(chunks_per_call)

    def astream(self, *args, **kwargs):
        chunks = self._per_call.pop(0) if self._per_call else []
        return _source(chunks)


class _FakeInvokeAgent:
    def __init__(self, result):
        self._result = result

    async def ainvoke(self, *args, **kwargs):
        return self._result


def _gen_turn(*, chunks=None, chunks_per_call=None, invoke_result=None, no_continue=False):
    turn = m._GenerateTurn(
        SID,
        MultiModalMessage(text="q"),
        is_stream=chunks is not None or chunks_per_call is not None,
        origin=None,
    )

    async def _noop_prepare():
        return None

    turn._prepare = _noop_prepare
    if no_continue:
        # cap the retries so the detection under test is never re-streamed
        turn._continuation_retries = _MAX_CONTINUATION_RETRIES
    if chunks_per_call is not None:
        turn._agent = _FakeStreamAgent(chunks_per_call)
    elif chunks is not None:
        turn._agent = _FakeStreamAgent([chunks])
    else:
        turn._agent = _FakeInvokeAgent(invoke_result)
    return turn


async def _collect(agen) -> list[dict]:
    return [f async for f in agen]


@pytest.fixture(autouse=True)
def _reset_stream_flag():
    yield
    state_register_mem.delete_state(SID, "is_stream_turn")


@pytest.fixture(autouse=True)
def _fixed_base(monkeypatch):
    monkeypatch.setattr(mtb, "_BASE_MAX_TOKENS", 8192)


# ---- Phase 1: finish_reason detection --------------------------------------


class TestFinishReasonDetection:
    def test_stream_finish_reason_length(self):
        turn = _gen_turn(
            no_continue=True,
            chunks=[
                _mc(
                    _chunked({"content": "partial", "response_metadata": {"finish_reason": "stop"}})
                ),
                _mc(
                    _chunked(
                        {
                            "content": "",
                            "response_metadata": {"finish_reason": "length"},
                        }
                    )
                ),
            ],
        )
        asyncio.run(_collect(turn.run()))
        assert turn.meta_finish_reason == "length"

    def test_stream_finish_reason_stop(self):
        turn = _gen_turn(
            no_continue=True,
            chunks=[
                _mc(_chunked({"content": "done", "response_metadata": {"finish_reason": "stop"}}))
            ],
        )
        asyncio.run(_collect(turn.run()))
        assert turn.meta_finish_reason == "stop"

    def test_stream_stop_reason_max_tokens_anthropic(self):
        turn = _gen_turn(
            no_continue=True,
            chunks=[
                _mc(_chunked({"content": "", "response_metadata": {"stop_reason": "max_tokens"}}))
            ],
        )
        asyncio.run(_collect(turn.run()))
        assert turn.meta_finish_reason == "max_tokens"

    def test_invoke_finish_reason_extracted(self):
        last = AIMessage(
            content="final", response_metadata={"finish_reason": "length"}, tool_calls=[]
        )
        turn = _gen_turn(no_continue=True, invoke_result={"messages": [last]})
        frames = asyncio.run(_collect(turn.run()))
        assert frames[0] == {"type": "text", "content": "final"}
        assert turn.meta_finish_reason == "length"

    def test_meta_chunk_includes_finish_reason(self):
        turn = _gen_turn(
            no_continue=True,
            chunks=[
                _mc(_chunked({"content": "x", "response_metadata": {"finish_reason": "length"}}))
            ],
        )
        frames = asyncio.run(_collect(turn.run()))
        meta = [f for f in frames if f.get("type") == "meta"]
        assert len(meta) == 1
        assert meta[0]["finish_reason"] == "length"

    def test_meta_chunk_finish_reason_stop_on_normal(self):
        turn = _gen_turn(
            no_continue=True,
            chunks=[
                _mc(_chunked({"content": "x", "response_metadata": {"finish_reason": "stop"}}))
            ],
        )
        frames = asyncio.run(_collect(turn.run()))
        meta = [f for f in frames if f.get("type") == "meta"]
        assert meta[0]["finish_reason"] == "stop"

    def test_has_tool_calls_set_on_tool_call_chunk(self):
        turn = _gen_turn(
            no_continue=True,
            chunks=[
                _mc(_tool_chunk()),
                _mc(_chunked({"content": "", "response_metadata": {"finish_reason": "length"}})),
            ],
        )
        asyncio.run(_collect(turn.run()))
        assert turn._has_tool_calls is True

    def test_has_tool_calls_false_on_text_only(self):
        turn = _gen_turn(
            no_continue=True,
            chunks=[
                _mc(
                    _chunked(
                        {"content": "text only", "response_metadata": {"finish_reason": "stop"}}
                    )
                )
            ],
        )
        asyncio.run(_collect(turn.run()))
        assert turn._has_tool_calls is False


# ---- Phase 2: text continuation --------------------------------------------


class TestTextContinuation:
    def _turn(self, finish="length", tool_calls=False, retries=0):
        turn = _gen_turn(chunks=[])
        turn.meta_finish_reason = finish
        turn._has_tool_calls = tool_calls
        turn._continuation_retries = retries
        return turn

    def test_should_continue_on_length_no_tool_calls(self):
        assert self._turn(finish="length")._should_text_continue() is True

    def test_should_continue_on_max_tokens_no_tool_calls(self):
        assert self._turn(finish="max_tokens")._should_text_continue() is True

    def test_should_not_continue_on_stop(self):
        assert self._turn(finish="stop")._should_text_continue() is False

    def test_should_not_continue_with_tool_calls(self):
        assert self._turn(finish="length", tool_calls=True)._should_text_continue() is False

    def test_should_not_continue_after_max_retries(self):
        turn = self._turn(finish="length", retries=_MAX_CONTINUATION_RETRIES)
        assert turn._should_text_continue() is False

    def test_prepare_continuation_sets_flag_and_increments(self):
        turn = self._turn()
        turn.meta_finish_reason = "length"
        turn._has_tool_calls = True
        turn._prepare_continuation()
        assert turn._is_continuation is True
        assert turn._continuation_retries == 1
        assert turn.meta_finish_reason is None
        assert turn._has_tool_calls is False

    def test_create_source_uses_continuation_prompt(self):
        class _SpyAgent:
            def __init__(self):
                self.kwargs = None

            def astream(self, **kwargs):
                self.kwargs = kwargs
                return _source([])

        turn = _gen_turn(chunks=[])
        turn._is_continuation = True
        spy = _SpyAgent()
        turn._agent = spy

        kind, _src = asyncio.run(turn._create_source())

        assert kind == "stream"
        messages = spy.kwargs["input"]["messages"]
        assert len(messages) == 1
        assert messages[0].content == _CONTINUATION_PROMPT

    def test_continuation_retries_reset_per_turn(self):
        turn = _gen_turn(chunks=[])
        assert turn._continuation_retries == 0
        assert turn._is_continuation is False


# ---- Phase 3: MaxTokensBoostMiddleware --------------------------------------


def _ai(finish, tool_calls=None):
    return AIMessage(
        content="resp",
        response_metadata={"finish_reason": finish},
        tool_calls=tool_calls or [],
    )


def _request(state=None, config=None, model_settings=None):
    class _Req:
        def __init__(self):
            self.state = state if state is not None else {"session_id": SID}
            self.config = config if config is not None else {}
            self.model_settings = model_settings if model_settings is not None else {}

    return _Req()


class TestMaxTokensBoostMiddleware:
    def _mw(self):
        return MaxTokensBoostMiddleware()

    def test_no_retry_on_normal_stop(self):
        calls = []

        async def handler(req):
            calls.append(req)
            return _ai("stop")

        req = _request()
        result = asyncio.run(self._mw().awrap_model_call(req, handler))
        assert len(calls) == 1
        assert result is not None
        assert "max_tokens" not in req.model_settings

    def test_no_retry_on_text_truncation_no_tool_calls(self):
        calls = []

        def handler(req):
            calls.append(req)
            return _ai("length")

        req = _request()
        self._mw().wrap_model_call(req, handler)
        assert len(calls) == 1

    def test_boost_capped_at_max_cap(self):
        truncated = _ai("length", tool_calls=[{"name": "f", "args": {}, "id": "1"}])

        def handler(req):
            return truncated

        req = _request()
        self._mw().wrap_model_call(req, handler)
        # retries exhausted: the last injected boost is the 32768 cap
        assert req.model_settings["max_tokens"] == mtb._MAX_CAP

    def test_truncated_result_returned_on_exhaustion(self):
        truncated = _ai("length", tool_calls=[{"name": "f", "args": {}, "id": "1"}])
        count = {"n": 0}

        async def handler(req):
            count["n"] += 1
            return truncated

        result = asyncio.run(self._mw().awrap_model_call(_request(), handler))
        assert count["n"] == 4  # 1 initial + 3 retries
        assert result is truncated

    # ---- non-streaming re-call ----

    def test_non_stream_re_call_on_tool_call_truncation(self):
        truncated = _ai("length", tool_calls=[{"name": "f", "args": {}, "id": "1"}])
        ok = _ai("stop", tool_calls=[{"name": "f", "args": {"x": 1}, "id": "1"}])
        results = [truncated, ok]

        async def handler(req):
            return results.pop(0)

        result = asyncio.run(self._mw().awrap_model_call(_request(), handler))
        assert result is ok

    def test_non_stream_progressive_boost(self):
        boosts = []
        truncated = _ai("length", tool_calls=[{"name": "f", "args": {}, "id": "1"}])
        ok = _ai("stop", tool_calls=[])
        calls = {"n": 0}

        async def handler(req):
            calls["n"] += 1
            if "max_tokens" in req.model_settings:
                boosts.append(req.model_settings["max_tokens"])
            return ok if calls["n"] >= 3 else truncated

        asyncio.run(self._mw().awrap_model_call(_request(), handler))
        assert boosts == [16384, 32768]

    def test_non_stream_max_3_retries(self):
        truncated = _ai("length", tool_calls=[{"name": "f", "args": {}, "id": "1"}])
        count = {"n": 0}

        def handler(req):
            count["n"] += 1
            return truncated

        self._mw().wrap_model_call(_request(), handler)
        assert count["n"] == 4

    def test_non_stream_success_returns_second_result(self):
        truncated = _ai("length", tool_calls=[{"name": "f", "args": {}, "id": "1"}])
        ok = _ai("stop", tool_calls=[])
        results = [truncated, ok]

        async def handler(req):
            return results.pop(0)

        assert asyncio.run(self._mw().awrap_model_call(_request(), handler)) is ok

    # ---- streaming re-call + callback stripping ----

    def _stream_setup(self, callbacks_sentinel=object()):
        req = _request(config={"callbacks": callbacks_sentinel})
        state_register_mem.set_state(SID, "is_stream_turn", True)
        return req, callbacks_sentinel

    def test_stream_re_call_on_tool_call_truncation(self):
        truncated = _ai("length", tool_calls=[{"name": "f", "args": {}, "id": "1"}])
        ok = _ai("stop", tool_calls=[])
        results = [truncated, ok]
        calls = []

        async def handler(req):
            calls.append(1)
            return results.pop(0)

        req, _ = self._stream_setup()
        result = asyncio.run(self._mw().awrap_model_call(req, handler))
        assert len(calls) == 2
        assert result is ok

    def test_stream_strips_callbacks_during_re_call(self):
        sentinel = object()
        seen = []

        async def handler(req):
            seen.append(req.config.get("callbacks"))
            return (
                _ai("stop")
                if len(seen) > 1
                else _ai("length", tool_calls=[{"name": "f", "args": {}, "id": "1"}])
            )

        req, _ = self._stream_setup(sentinel)
        asyncio.run(self._mw().awrap_model_call(req, handler))
        assert seen[0] is sentinel
        assert seen[1] is None

    def test_stream_restores_callbacks_after_re_call(self):
        sentinel = object()

        async def handler(req):
            return (
                _ai("stop")
                if req.config.get("callbacks") is None
                else _ai("length", tool_calls=[{"name": "f", "args": {}, "id": "1"}])
            )

        req, _ = self._stream_setup(sentinel)
        asyncio.run(self._mw().awrap_model_call(req, handler))
        assert req.config.get("callbacks") is sentinel

    def test_stream_restores_callbacks_even_on_exception(self):
        sentinel = object()

        async def handler(req):
            if req.config.get("callbacks") is None:
                raise RuntimeError("boom")
            return _ai("length", tool_calls=[{"name": "f", "args": {}, "id": "1"}])

        req, _ = self._stream_setup(sentinel)
        try:
            asyncio.run(self._mw().awrap_model_call(req, handler))
        except RuntimeError:  # noqa: S110
            pass
        assert req.config.get("callbacks") is sentinel

    def test_stream_callbacks_not_stripped_on_first_call(self):
        sentinel = object()

        async def handler(req):
            return _ai("stop")

        req, _ = self._stream_setup(sentinel)
        asyncio.run(self._mw().awrap_model_call(req, handler))
        assert req.config.get("callbacks") is sentinel

    def test_stream_progressive_boost(self):
        boosts = []
        truncated = _ai("length", tool_calls=[{"name": "f", "args": {}, "id": "1"}])
        ok = _ai("stop", tool_calls=[])
        calls = {"n": 0}

        async def handler(req):
            calls["n"] += 1
            if "max_tokens" in req.model_settings:
                boosts.append(req.model_settings["max_tokens"])
            return ok if calls["n"] >= 3 else truncated

        req, _ = self._stream_setup()
        asyncio.run(self._mw().awrap_model_call(req, handler))
        assert boosts == [16384, 32768]

    def test_stream_max_3_retries(self):
        truncated = _ai("length", tool_calls=[{"name": "f", "args": {}, "id": "1"}])
        count = {"n": 0}

        async def handler(req):
            count["n"] += 1
            return truncated

        req, _ = self._stream_setup()
        asyncio.run(self._mw().awrap_model_call(req, handler))
        assert count["n"] == 4

    def test_stream_success_returns_second_result(self):
        truncated = _ai("length", tool_calls=[{"name": "f", "args": {}, "id": "1"}])
        ok = _ai("stop", tool_calls=[])
        results = [truncated, ok]

        async def handler(req):
            return results.pop(0)

        req, _ = self._stream_setup()
        assert asyncio.run(self._mw().awrap_model_call(req, handler)) is ok

    def test_stream_no_retry_on_normal_stop(self):
        calls = []

        async def handler(req):
            calls.append(1)
            return _ai("stop")

        req, _ = self._stream_setup()
        asyncio.run(self._mw().awrap_model_call(req, handler))
        assert len(calls) == 1
        assert req.config.get("callbacks") is not None

    def test_stream_no_retry_on_text_truncation_no_tool_calls(self):
        calls = []

        async def handler(req):
            calls.append(1)
            return _ai("length")

        req, _ = self._stream_setup()
        asyncio.run(self._mw().awrap_model_call(req, handler))
        assert len(calls) == 1


# ---- Phase 2 + Phase 3 integration ------------------------------------------


class TestPhase2Phase3Integration:
    def test_text_truncation_triggers_phase2_not_phase3(self):
        turn = _gen_turn(
            chunks_per_call=[
                [
                    _mc(
                        _chunked(
                            {
                                "content": "half answer",
                                "response_metadata": {"finish_reason": "length"},
                            }
                        )
                    )
                ],
                [],
            ]
        )
        asyncio.run(_collect(turn.run()))
        # run() drove the continuation loop: one re-stream happened, then the
        # empty second round produced no finish_reason → loop stopped.
        assert turn._continuation_retries == 1
        assert turn._is_continuation is True

    def test_tool_call_truncation_does_not_trigger_phase2(self):
        turn = _gen_turn(
            chunks=[
                _mc(_tool_chunk()),
                _mc(_chunked({"content": "", "response_metadata": {"finish_reason": "length"}})),
            ]
        )
        asyncio.run(_collect(turn.run()))
        assert turn._should_text_continue() is False

    def test_stop_does_not_trigger_either_phase(self):
        turn = _gen_turn(
            chunks=[
                _mc(_chunked({"content": "done", "response_metadata": {"finish_reason": "stop"}}))
            ]
        )
        frames = asyncio.run(_collect(turn.run()))
        assert turn._should_text_continue() is False
        assert [f for f in frames if f.get("type") == "meta"][0]["finish_reason"] == "stop"
