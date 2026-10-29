"""Anthropic ``cache_control`` breakpoints: placement, gating and fail-open.

Anthropic caches nothing unless the request marks a prefix, so this wrapper is
the only thing that turns sherry's frozen prompt architecture into cache hits on
that provider. The tests pin the three breakpoints (system / last tool / settled
history tail), the provider gate, the budget, the TTL knob, and the promise that
a failure or a disable is a byte-for-byte pass-through.
"""

from __future__ import annotations

import json
from typing import Any
from collections.abc import AsyncIterator, Iterator, Sequence

import pytest
from langchain_core.callbacks import (
    AsyncCallbackManagerForLLMRun,
    CallbackManagerForLLMRun,
)
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import (
    AIMessage,
    AIMessageChunk,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)
from langchain_core.outputs import ChatGeneration, ChatGenerationChunk, ChatResult
from pydantic import Field

from config.features import ANTHROPIC_CACHE
from models.LLMs.anthropic_cache import (
    AnthropicCacheControlChatModel,
    cache_control_value,
    inject_breakpoints,
    is_anthropic_provider,
    maybe_wrap_anthropic_cache,
)

pytestmark = [pytest.mark.unit]

CC = {"type": "ephemeral"}


class _RecordingModel(BaseChatModel):
    """A chat model that records what it was asked to generate."""

    seen: list[Any] = Field(default_factory=list)

    @property
    def _llm_type(self) -> str:
        return "recording"

    def _generate(
        self,
        messages: list,
        stop: list[str] | None = None,
        run_manager: CallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        self.seen.append((messages, kwargs))
        return ChatResult(generations=[ChatGeneration(message=AIMessage(content="ok"))])

    async def _agenerate(
        self,
        messages: list,
        stop: list[str] | None = None,
        run_manager: AsyncCallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        self.seen.append((messages, kwargs))
        return ChatResult(generations=[ChatGeneration(message=AIMessage(content="ok"))])

    def _stream(
        self,
        messages: list,
        stop: list[str] | None = None,
        run_manager: CallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> Iterator[ChatGenerationChunk]:
        self.seen.append((messages, kwargs))
        yield ChatGenerationChunk(message=AIMessageChunk(content="ok"))

    async def _astream(
        self,
        messages: list,
        stop: list[str] | None = None,
        run_manager: AsyncCallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> AsyncIterator[ChatGenerationChunk]:
        self.seen.append((messages, kwargs))
        yield ChatGenerationChunk(message=AIMessageChunk(content="ok"))


def _history(tail: Sequence[Any] | None = None) -> list:
    """System + one completed tool turn, optionally followed by turn-tail messages."""
    messages: list = [
        SystemMessage(content="frozen persona"),
        HumanMessage(content="first question"),
        AIMessage(
            content="",
            tool_calls=[{"id": "t1", "name": "read_file", "args": {"path": "a.py"}}],
        ),
        ToolMessage(content="file body", tool_call_id="t1"),
    ]
    if tail:
        messages.extend(tail)
    return messages


def _tools(n: int = 2) -> list[dict]:
    return [
        {
            "type": "function",
            "function": {
                "name": f"tool_{index}",
                "description": "probe",
                "parameters": {"type": "object", "properties": {}},
            },
        }
        for index in range(n)
    ]


def _marks(messages: Sequence[Any], tools: Any) -> int:
    total = 0
    for message in messages:
        content = getattr(message, "content", None)
        if isinstance(content, list):
            total += sum(1 for b in content if isinstance(b, dict) and b.get("cache_control"))
    if isinstance(tools, list):
        total += sum(1 for t in tools if isinstance(t, dict) and t.get("cache_control"))
    return total


class TestProviderGate:
    def test_only_the_native_anthropic_provider_counts(self):
        assert is_anthropic_provider("anthropic") is True
        assert is_anthropic_provider("Anthropic") is True
        assert is_anthropic_provider("openai") is False
        assert is_anthropic_provider("bedrock") is False
        assert is_anthropic_provider(None) is False

    def test_a_non_anthropic_model_is_returned_untouched(self):
        model = _RecordingModel()

        assert maybe_wrap_anthropic_cache(model, "deepseek") is model
        assert maybe_wrap_anthropic_cache(model, None) is model

    def test_the_anthropic_model_is_wrapped_once(self):
        model = _RecordingModel()

        wrapped = maybe_wrap_anthropic_cache(model, "anthropic")

        assert isinstance(wrapped, AnthropicCacheControlChatModel)
        assert maybe_wrap_anthropic_cache(wrapped, "anthropic") is wrapped

    def test_disabled_is_a_passthrough(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setitem(ANTHROPIC_CACHE, "enabled", False)
        model = _RecordingModel()

        assert maybe_wrap_anthropic_cache(model, "anthropic") is model
        assert cache_control_value() is None


class TestBreakpointPlacement:
    def test_all_three_breakpoints_are_placed(self):
        messages, tools, placed = inject_breakpoints(_history(), _tools())

        assert placed == 3
        assert messages[0].content[0]["cache_control"] == CC  # BP1 system
        assert all("cache_control" not in t for t in tools[:-1])
        assert tools[-1]["cache_control"] == CC  # BP2 last tool
        assert messages[-1].content[0]["cache_control"] == CC  # BP3 history tail

    def test_the_current_turn_input_is_never_marked(self):
        tail = [
            HumanMessage(content="<system-reminder>live state</system-reminder>"),
            HumanMessage(content="now edit it"),
        ]

        messages, _tools_out, placed = inject_breakpoints(_history(tail), _tools())

        assert placed == 3
        assert messages[3].content[0]["cache_control"] == CC  # the ToolMessage
        assert "cache_control" not in json.dumps(messages[4].content)
        assert "cache_control" not in json.dumps(messages[5].content)

    def test_a_pure_tool_call_assistant_turn_is_skipped_for_an_earlier_target(self):
        messages = [
            SystemMessage(content="persona"),
            HumanMessage(content="q1"),
            AIMessage(content="an earlier answer"),
            AIMessage(content="", tool_calls=[{"id": "t9", "name": "x", "args": {}}]),
        ]

        marked, _tools_out, _placed = inject_breakpoints(messages, [])

        # The tool-call-only turn cannot carry a breakpoint: the scan continues
        # back to the previous answer, which is still a reusable prefix.
        assert marked[2].content[0]["cache_control"] == CC
        assert marked[3].content == ""

    def test_the_caller_messages_are_not_mutated(self):
        original = _history()

        inject_breakpoints(original, _tools())

        assert isinstance(original[0].content, str), "the caller's SystemMessage was mutated"

    def test_existing_breakpoints_count_against_the_budget(self):
        messages = _history()
        messages.append(
            AIMessage(
                content=[{"type": "text", "text": "already", "cache_control": CC}],
            )
        )

        _marked, _tools_out, placed = inject_breakpoints(messages, _tools(), max_breakpoints=1)

        assert placed == 0, "one caller breakpoint plus a budget of 1 leaves no room"

    def test_the_budget_caps_a_request_with_many_breakpoint_candidates(self):
        messages, tools, placed = inject_breakpoints(_history(), _tools(), max_breakpoints=2)

        assert placed == 2
        assert _marks(messages, tools) == 2

    def test_an_empty_budget_marks_nothing(self):
        messages, tools, placed = inject_breakpoints(_history(), _tools(), max_breakpoints=0)

        assert placed == 0 and _marks(messages, tools) == 0


class TestTtl:
    def test_short_is_the_bare_ephemeral_marker(self):
        assert cache_control_value(ttl="short") == {"type": "ephemeral"}

    def test_long_adds_the_one_hour_ttl(self):
        value = cache_control_value(ttl="long")

        assert value == {"type": "ephemeral", "ttl": "1h"}
        marked, _tools_out, _placed = inject_breakpoints(_history(), _tools(), value=value)
        assert marked[0].content[0]["cache_control"] == value


class TestWrapperDelegation:
    def test_a_generate_receives_marked_messages(self):
        inner = _RecordingModel()
        wrapper = AnthropicCacheControlChatModel(inner=inner)

        wrapper._generate(_history(), stop=None, run_manager=None, tools=_tools())

        messages, kwargs = inner.seen[-1]
        assert _marks(messages, kwargs.get("tools")) == 3
        assert kwargs["tools"][-1]["cache_control"] == CC

    @pytest.mark.asyncio
    async def test_async_generation_receives_marked_messages(self):
        inner = _RecordingModel()
        wrapper = AnthropicCacheControlChatModel(inner=inner)

        await wrapper._agenerate(_history(), stop=None, run_manager=None, tools=_tools())

        messages, kwargs = inner.seen[-1]
        assert _marks(messages, kwargs.get("tools")) == 3

    @pytest.mark.asyncio
    async def test_streaming_receives_marked_messages(self):
        inner = _RecordingModel()
        wrapper = AnthropicCacheControlChatModel(inner=inner)

        chunks = [chunk async for chunk in wrapper._astream(_history(), run_manager=None)]

        assert chunks and chunks[0].message.content == "ok"
        messages, _kwargs = inner.seen[-1]
        assert _marks(messages, None) == 2  # no tools bound in this call

    def test_identity_mirrors_the_inner_model(self):
        inner = _RecordingModel()
        wrapper = AnthropicCacheControlChatModel(inner=inner)

        assert wrapper._llm_type.endswith(inner._llm_type)
        assert wrapper._identifying_params["inner_type"] == inner._llm_type

    def test_structured_output_delegates_to_the_provider_implementation(self):
        """ChatAnthropic's strategy must not be replaced by the generic one."""
        from langchain_anthropic import ChatAnthropic

        inner = ChatAnthropic(model="claude-sonnet-4-20250514", api_key="sk-test", max_tokens=64)
        wrapper = AnthropicCacheControlChatModel(inner=inner)

        schema = {
            "title": "probe",
            "type": "object",
            "properties": {"answer": {"type": "string"}},
        }
        bound = wrapper.with_structured_output(schema)

        # The delegated runnable is built on the INNER model (its own strategy),
        # not on the wrapper: the sequence's model step is the provider client.
        first = getattr(bound, "first", None)
        bound_model = getattr(first, "bound", None)
        assert bound_model is inner
        assert bound_model is not wrapper

    def test_an_injection_error_sends_the_request_unchanged(self, monkeypatch: pytest.MonkeyPatch):
        import models.LLMs.anthropic_cache as module

        def _boom(*_args, **_kwargs):
            raise RuntimeError("injection exploded")

        monkeypatch.setattr(module, "inject_breakpoints", _boom)
        inner = _RecordingModel()
        wrapper = AnthropicCacheControlChatModel(inner=inner)
        original = _history()

        wrapper._generate(original, run_manager=None, tools=_tools())

        messages, kwargs = inner.seen[-1]
        assert _marks(messages, kwargs.get("tools")) == 0
        assert isinstance(messages[0].content, str)


class TestWirePayload:
    """The final Anthropic request must carry the markers (no network involved)."""

    def test_the_payload_carries_all_three_breakpoints(self):
        from langchain_anthropic import ChatAnthropic

        inner = ChatAnthropic(model="claude-sonnet-4-20250514", api_key="sk-test", max_tokens=64)
        marked, tools, placed = inject_breakpoints(_history(), _tools())

        payload = inner._get_request_payload(marked, tools=tools)

        assert placed == 3
        assert payload["system"][-1]["cache_control"] == CC
        assert payload["tools"][-1]["cache_control"] == CC
        tool_results = [
            b
            for m in payload["messages"]
            for b in _blocks(m.get("content"))
            if b.get("type") == "tool_result"
        ]
        assert tool_results and tool_results[-1]["cache_control"] == CC


def _blocks(content: Any) -> list[dict]:
    return [b for b in content if isinstance(b, dict)] if isinstance(content, list) else []
