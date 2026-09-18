"""Integration tests: P1-2 tail clip inside the Summarization middleware.

Pins the real dispatch shapes (T1 preflight / T2 pre-call / T4-T5 provider
recovery), sync and async:

- a sufficient clip recovers WITHOUT any LLM summary call and short-circuits
  the existing route (no compact, no budget truncation);
- an insufficient clip degrades to the existing compact route (LLM runs);
- the clipped request keeps tool-call/result pairing sanitizer-clean and
  unchanged;
- the T4/T5 recovery loop retries on the clip once, then degrades to forced
  compression (a stubbed request makes the clip a no-op by design);
- the config kill switch restores the pre-P1-2 behavior.

Window math: CTX_WINDOW 41600 − COMPRESSION_RESERVE_TOKENS 16000 = usable
25600; threshold_truncate = 17920; threshold_compact = 20480.
"""

from __future__ import annotations

import asyncio
import contextlib
import uuid

import pytest
from loguru import logger
from langchain.agents.middleware import ModelRequest
from langchain_core.messages import AIMessage, HumanMessage, RemoveMessage, ToolMessage
from langgraph.graph.message import REMOVE_ALL_MESSAGES
from types import SimpleNamespace

import agent.middlewares.summarization.core as summarization_module
from config.features import SUMMARIZATION
from pub.func.message.overflow_clip import CLIP_MARKER
from pub.func.transcript_repair import sanitize_tool_use_result_pairing
from runtime import state_register_mem

CTX_WINDOW = 41_600
USABLE_BUDGET = CTX_WINDOW - 16_000

pytestmark = [pytest.mark.module]


class StubModel:
    _llm_type = "fake"

    def __init__(self):
        self.text = (
            "The assistant completed the analysis and decided on a final approach for the task."
        )
        self.calls: list = []

    def invoke(self, prompt, config=None):
        self.calls.append(prompt)
        return SimpleNamespace(text=self.text)

    async def ainvoke(self, prompt, config=None):
        self.calls.append(prompt)
        return SimpleNamespace(text=self.text)


class ProviderContextOverflowError(Exception):
    """Context-overflow-shaped provider error (classifier channel 2)."""


def make_request(messages, session_id, model):
    return ModelRequest(
        model=model,
        messages=list(messages),
        state={"session_id": session_id, "messages": list(messages)},
    )


def make_middleware(model):
    return summarization_module.Summarization(
        model=model,
        trigger=[("tokens", 80_000)],
        keep=("messages", 10),
        main_llm_context_window=CTX_WINDOW,
        need_update_system_prompt=False,
    )


def mget(name):
    return getattr(summarization_module, name)


@pytest.fixture
def sid(request):
    session = "tclip-" + request.node.name[:40] + "-" + uuid.uuid4().hex[:6]
    yield session
    try:
        state_register_mem.clear_session(session)
    except Exception:  # noqa: S110
        pass


@contextlib.contextmanager
def capture_logs(level="INFO"):
    lines: list[str] = []
    handler_id = logger.add(lines.append, level=level, format="{message}")
    try:
        yield lines
    finally:
        logger.remove(handler_id)


def _ai_tool_call(*call_ids: str) -> AIMessage:
    return AIMessage(
        content="",
        tool_calls=[
            {"name": "search", "args": {"q": call_id}, "id": call_id} for call_id in call_ids
        ],
    )


def clip_recoverable_messages() -> list:
    """est 55000 >= threshold_compact, no truncatable candidate (tail batch).

    All three ToolMessages sit inside the TRUNCATABLE_RECENT_SKIP window, so
    the existing route would be compact_only; the tail clip alone drops the
    estimate to ~10000 < 20480.
    """
    return [
        HumanMessage(content="h" * 40_000),
        AIMessage(content="a1"),
        HumanMessage(content="q2"),
        _ai_tool_call("c1", "c2", "c3"),
        ToolMessage(content="x" * 60_000, tool_call_id="c1"),
        ToolMessage(content="y" * 60_000, tool_call_id="c2"),
        ToolMessage(content="z" * 60_000, tool_call_id="c3"),
    ]


def clip_insufficient_messages() -> list:
    """est 55000; the unclippable HumanMessage keeps it >= 20480 after the clip."""
    return [
        HumanMessage(content="h" * 100_000),
        AIMessage(content="a1"),
        HumanMessage(content="q2"),
        _ai_tool_call("c1", "c2"),
        ToolMessage(content="x" * 60_000, tool_call_id="c1"),
        ToolMessage(content="y" * 60_000, tool_call_id="c2"),
    ]


def forced_recovery_messages() -> list:
    """est ~10000 < threshold_truncate -> route fits; provider still overflows."""
    return [
        HumanMessage(content="q"),
        AIMessage(content="a1"),
        HumanMessage(content="q2"),
        _ai_tool_call("c1", "c2"),
        ToolMessage(content="x" * 20_000, tool_call_id="c1"),
        ToolMessage(content="y" * 20_000, tool_call_id="c2"),
    ]


class TestT2FastPath:
    def test_sufficient_clip_recovers_without_llm(self, sid):
        stub = StubModel()
        mw = make_middleware(stub)
        captured: dict = {}

        def handler(request):
            captured["messages"] = list(request.messages)
            return AIMessage(content="ok")

        with capture_logs() as lines:
            response = mw.wrap_model_call(
                make_request(clip_recoverable_messages(), sid, stub), handler
            )

        assert response.content == "ok"
        assert stub.calls == []
        clipped = captured["messages"]
        assert len(clipped) == 7
        assert all(CLIP_MARKER in message.content for message in clipped[4:])
        assert clipped[0].content == "h" * 40_000
        assert any("route=tail_clip" in line and "trigger=T2" in line for line in lines)
        assert not any("route=compact" in line for line in lines)
        assert state_register_mem.get_state(sid, mget("_COMPRESSION_COUNT_KEY")) in (None, 0)
        assert state_register_mem.get_state(sid, mget("_COOLDOWN_ROUNDS_KEY")) in (None, 0)

    def test_insufficient_clip_degrades_to_existing_route(self, sid):
        stub = StubModel()
        mw = make_middleware(stub)

        with capture_logs() as lines:
            mw.wrap_model_call(
                make_request(clip_insufficient_messages(), sid, stub),
                lambda request: AIMessage(content="ok"),
            )

        assert stub.calls != []
        assert state_register_mem.get_state(sid, mget("_COMPRESSION_COUNT_KEY")) == 1
        assert any("route=compact_only" in line for line in lines)
        assert not any("route=tail_clip" in line for line in lines)

    def test_disabled_kill_switch_falls_back_to_compact(self, sid, monkeypatch):
        monkeypatch.setitem(SUMMARIZATION, "overflow_clip_enabled", False)
        stub = StubModel()
        mw = make_middleware(stub)

        with capture_logs() as lines:
            mw.wrap_model_call(
                make_request(clip_recoverable_messages(), sid, stub),
                lambda request: AIMessage(content="ok"),
            )

        assert state_register_mem.get_state(sid, mget("_COMPRESSION_COUNT_KEY")) == 1
        assert any("route=compact_only" in line for line in lines)
        assert not any("route=tail_clip" in line for line in lines)


class TestT1FastPath:
    def test_t1_clip_recovers_via_state_update_without_llm(self, sid):
        stub = StubModel()
        mw = make_middleware(stub)

        with capture_logs() as lines:
            result = mw.before_agent(
                {"session_id": sid, "messages": clip_recoverable_messages()}, None
            )

        assert result is not None
        assert isinstance(result["messages"][0], RemoveMessage)
        assert result["messages"][0].id == REMOVE_ALL_MESSAGES
        rebuilt = list(result["messages"][1:])
        assert len(rebuilt) == 7
        assert all(CLIP_MARKER in message.content for message in rebuilt[4:])
        assert stub.calls == []
        assert any("route=tail_clip" in line and "trigger=T1" in line for line in lines)


class TestForcedRecovery:
    def test_overflow_error_clips_then_retries_without_llm(self, sid):
        stub = StubModel()
        mw = make_middleware(stub)
        requests: list = []
        calls = {"n": 0}

        def handler(request):
            calls["n"] += 1
            requests.append(list(request.messages))
            if calls["n"] == 1:
                raise ProviderContextOverflowError(
                    "This model's maximum context length is 41600 tokens"
                )
            return AIMessage(content="recovered")

        with capture_logs() as lines:
            response = mw.wrap_model_call(
                make_request(forced_recovery_messages(), sid, stub), handler
            )

        assert response.content == "recovered"
        assert calls["n"] == 2
        assert stub.calls == []
        assert all(CLIP_MARKER in message.content for message in requests[1][4:])
        assert state_register_mem.get_state(sid, mget("_OVERFLOW_RETRIES_KEY")) == 1
        assert any("route=tail_clip" in line and "trigger=T5" in line for line in lines)

    def test_repeated_overflow_degrades_to_forced_compression(self, sid):
        stub = StubModel()
        mw = make_middleware(stub)
        requests: list = []
        calls = {"n": 0}

        def handler(request):
            calls["n"] += 1
            requests.append(list(request.messages))
            if calls["n"] <= 2:
                raise ProviderContextOverflowError(
                    "This message exceeds the maximum context length of the model"
                )
            return AIMessage(content="recovered")

        with capture_logs() as lines:
            response = mw.wrap_model_call(
                make_request(forced_recovery_messages(), sid, stub), handler
            )

        assert response.content == "recovered"
        assert calls["n"] == 3
        assert state_register_mem.get_state(sid, mget("_OVERFLOW_RETRIES_KEY")) == 2
        assert sum(1 for line in lines if "route=tail_clip" in line) == 1
        # second attempt skipped the (idempotent) clip and ran compression
        assert any("trigger=T5, attempt=2/3" in line for line in lines)


class TestAsyncParity:
    def test_awrap_clip_fast_path_matches_sync(self, sid):
        stub = StubModel()
        mw = make_middleware(stub)
        captured: dict = {}

        async def ahandler(request):
            captured["messages"] = list(request.messages)
            return AIMessage(content="ok")

        with capture_logs() as lines:
            response = asyncio.run(
                mw.awrap_model_call(make_request(clip_recoverable_messages(), sid, stub), ahandler)
            )

        assert response.content == "ok"
        assert stub.calls == []
        assert len(captured["messages"]) == 7
        assert all(CLIP_MARKER in message.content for message in captured["messages"][4:])
        assert any("route=tail_clip" in line and "trigger=T2" in line for line in lines)

    def test_aforced_recovery_clip_matches_sync(self, sid):
        stub = StubModel()
        mw = make_middleware(stub)
        calls = {"n": 0}

        async def ahandler(request):
            calls["n"] += 1
            if calls["n"] == 1:
                raise ProviderContextOverflowError(
                    "This model's maximum context length is 41600 tokens"
                )
            return AIMessage(content="recovered")

        with capture_logs() as lines:
            response = asyncio.run(
                mw.awrap_model_call(make_request(forced_recovery_messages(), sid, stub), ahandler)
            )

        assert response.content == "recovered"
        assert calls["n"] == 2
        assert stub.calls == []
        assert any("route=tail_clip" in line and "trigger=T5" in line for line in lines)


class TestPairingInvariant:
    def test_clipped_request_passes_sanitizer_unchanged(self, sid):
        stub = StubModel()
        mw = make_middleware(stub)
        original = clip_recoverable_messages()
        captured: dict = {}

        def handler(request):
            captured["messages"] = request.messages
            return AIMessage(content="ok")

        with capture_logs():
            mw.wrap_model_call(make_request(original, sid, stub), handler)

        clipped = captured["messages"]
        sanitized = sanitize_tool_use_result_pairing(clipped)
        assert sanitized is clipped
        structure_clipped = [
            (type(message).__name__, getattr(message, "tool_call_id", None))
            for message in sanitized
        ]
        structure_original = [
            (type(message).__name__, getattr(message, "tool_call_id", None))
            for message in sanitize_tool_use_result_pairing(list(original))
        ]
        assert structure_clipped == structure_original
        assert not any(
            "tool result missing after context trim." in str(message.content)
            for message in sanitized
        )
