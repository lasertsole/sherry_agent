"""Unit tests for NormalizingChatModel's streaming DELTA contract.

The normalizer must forward per-chunk reasoning DELTAS verbatim (renaming
provider alias keys onto ``reasoning_content``) WITHOUT accumulation:

1. The client appends every ``{"type": "reasoning"}`` chunk — cumulative
   values would duplicate the thinking text on the wire.
2. LangChain aggregates streamed chunks via ``AIMessageChunk.__add__`` whose
   ``merge_dicts`` CONCATENATES string ``additional_kwargs`` values — deltas
   therefore reconstruct the complete chain-of-thought on the final
   aggregated message, while cumulative values would O(n²)-duplicate it.

Regression context: the normalizer previously wrote cumulative text per
chunk, which produced an O(n²)-duplicated reasoning blob on the final
message and broke post-turn persistence of the thinking bubble.
"""

import asyncio

import pytest
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, AIMessageChunk, BaseMessage, HumanMessage
from langchain_core.outputs import ChatGeneration, ChatGenerationChunk, ChatResult
from typing import Any
from collections.abc import Iterator

from models.LLMs.reasoning_normalizer import NormalizingChatModel


class _FakeStreamingModel(BaseChatModel):
    """Emits canned per-chunk reasoning DELTAS (DeepSeek/GLM native shape)."""

    deltas: list[dict[str, str]] = []

    @property
    def _llm_type(self) -> str:
        return "fake-streaming"

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: Any = None,
        **kwargs: Any,
    ) -> ChatResult:
        full = "".join(d["reasoning_content"] for d in self.deltas)
        msg = AIMessage(content="", additional_kwargs={"reasoning_content": full})
        return ChatResult(generations=[ChatGeneration(message=msg)])

    def _stream(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: Any = None,
        **kwargs: Any,
    ) -> Iterator[ChatGenerationChunk]:
        for d in self.deltas:
            chunk = AIMessageChunk(content="", additional_kwargs=dict(d))
            yield ChatGenerationChunk(message=chunk)


def _make_model(deltas: list[dict[str, str]]) -> NormalizingChatModel:
    return NormalizingChatModel(inner=_FakeStreamingModel(deltas=deltas))


def _sync_stream_chunks(model: NormalizingChatModel) -> list[AIMessageChunk]:
    config = {"callbacks": None}
    return [
        gen.message
        for gen in model._stream(messages=[], config=config)  # type: ignore[arg-type]
    ]


pytestmark = [pytest.mark.unit]


class TestChunkDeltaPassthrough:
    def test_chunks_carry_deltas_verbatim(self):
        model = _make_model(
            [
                {"reasoning_content": "The"},
                {"reasoning_content": " user"},
                {"reasoning_content": " asks"},
            ]
        )
        chunks = _sync_stream_chunks(model)
        assert [c.additional_kwargs["reasoning_content"] for c in chunks] == [
            "The",
            " user",
            " asks",
        ]

    def test_no_cumulative_accumulation(self):
        deltas = [{"reasoning_content": d} for d in ["a", "ab", "abc"]]
        model = _make_model(deltas)
        chunks = _sync_stream_chunks(model)
        # If the normalizer accumulated, later chunks would repeat earlier text.
        assert [c.additional_kwargs["reasoning_content"] for c in chunks] == ["a", "ab", "abc"]

    def test_alias_keys_folded_into_canonical(self):
        model = _make_model(
            [{"reasoning": "d1"}, {"reasoning_text": "d2"}, {"reasoning_content": "d3"}]
        )
        chunks = _sync_stream_chunks(model)
        assert [c.additional_kwargs["reasoning_content"] for c in chunks] == ["d1", "d2", "d3"]
        for c in chunks:
            assert "reasoning" not in c.additional_kwargs
            assert "reasoning_text" not in c.additional_kwargs

    def test_text_only_chunks_untouched(self):
        model = _make_model([])
        chunks = [
            gen.message
            for gen in model._stream(messages=[], config={"callbacks": None})  # type: ignore[arg-type]
        ]
        assert chunks == []


class TestAggregationReconstructsFullCoT:
    def test_sum_of_chunks_yields_clean_full_text(self):
        """AIMessageChunk.__add__ concat semantics + deltas = full CoT."""
        deltas = ["The user", " is asking", " a simple question."]
        model = _make_model([{"reasoning_content": d} for d in deltas])
        chunks = _sync_stream_chunks(model)
        final = chunks[0]
        for c in chunks[1:]:
            final = final + c
        reasoning = final.additional_kwargs["reasoning_content"]
        assert reasoning == "The user is asking a simple question."

    def test_cumulative_would_o2_duplicate_aggregation(self):
        """Documents WHY cumulative chunks were removed: concat explodes."""
        cumulative = ["The user", "The user is asking"]
        chunks = [
            AIMessageChunk(content="", additional_kwargs={"reasoning_content": c})
            for c in cumulative
        ]
        final = chunks[0]
        for c in chunks[1:]:
            final = final + c
        assert final.additional_kwargs["reasoning_content"] == "The userThe user is asking"


class TestFullGenerationNormalization:
    def test_generate_message_keeps_full_reasoning(self):
        model = _make_model([{"reasoning_content": "a"}, {"reasoning_content": "b"}])
        result = model._generate(messages=[])
        msg = result.generations[0].message
        assert msg.additional_kwargs["reasoning_content"] == "ab"

    def test_generate_alias_normalized(self):
        class _AliasModel(_FakeStreamingModel):
            def _generate(self, messages, stop=None, run_manager=None, **kwargs):
                msg = AIMessage(content="", additional_kwargs={"reasoning_text": "aliased"})
                return ChatResult(generations=[ChatGeneration(message=msg)])

        model = NormalizingChatModel(inner=_AliasModel(deltas=[]))
        msg = model._generate(messages=[]).generations[0].message
        assert msg.additional_kwargs["reasoning_content"] == "aliased"
        assert "reasoning_text" not in msg.additional_kwargs


class TestAsyncStreamParity:
    @pytest.mark.asyncio
    async def test_astream_matches_sync_delta_semantics(self):
        model = _make_model([{"reasoning_content": "x"}, {"reasoning_content": "y"}])
        out = []
        async for gen in model._astream(messages=[], config={"callbacks": None}):  # type: ignore[arg-type]
            out.append(gen.message.additional_kwargs.get("reasoning_content"))
        assert out == ["x", "y"]


class _RecordingModel(BaseChatModel):
    """Records the kwargs each inner call receives."""

    generate_kwargs: list[dict[str, Any]] = []
    tool_bind_kwargs: list[dict[str, Any]] = []

    @property
    def _llm_type(self) -> str:
        return "fake-recording"

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: Any = None,
        **kwargs: Any,
    ) -> ChatResult:
        type(self).generate_kwargs.append(dict(kwargs))
        return ChatResult(generations=[ChatGeneration(message=AIMessage(content="ok"))])

    async def _agenerate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: Any = None,
        **kwargs: Any,
    ) -> ChatResult:
        type(self).generate_kwargs.append(dict(kwargs))
        return ChatResult(generations=[ChatGeneration(message=AIMessage(content="ok"))])

    def bind_tools(self, tools: Any, *, tool_choice: Any = None, **kwargs: Any) -> Any:
        type(self).tool_bind_kwargs.append(dict(kwargs))
        return self.bind(**kwargs)


class TestRequestOptions:
    """Forced request options must survive ``bind_tools`` (which drops the wrapper).

    JSON mode is the motivating case: the parameter must ride on every actual
    provider request, including the model calls the agent graph makes through a
    tool-bound Runnable.
    """

    def setup_method(self) -> None:
        _RecordingModel.generate_kwargs = []
        _RecordingModel.tool_bind_kwargs = []

    def test_a_copy_carries_the_options_and_the_original_stays_clean(self):
        original = NormalizingChatModel(inner=_RecordingModel())

        opted = original.with_request_options({"response_format": {"type": "json_object"}})

        assert original.request_options == {}
        assert opted.request_options == {"response_format": {"type": "json_object"}}

    def test_generate_forwards_the_options_to_the_inner_model(self):
        model = NormalizingChatModel(inner=_RecordingModel()).with_request_options(
            {"response_format": {"type": "json_object"}}
        )

        asyncio.run(model._agenerate([HumanMessage("hi")]))

        assert _RecordingModel.generate_kwargs[-1]["response_format"] == {"type": "json_object"}

    def test_bind_tools_marks_tools_strict_and_keeps_the_options_on_the_binding(self):
        model = NormalizingChatModel(inner=_RecordingModel()).with_request_options(
            {"response_format": {"type": "json_object"}}
        )

        bound = model.bind_tools([], tool_choice=None)

        # OpenAI's auto-parse path (taken when a response_format rides the
        # request) rejects non-strict tools.
        assert _RecordingModel.tool_bind_kwargs[-1] == {"strict": True}
        assert bound.kwargs["response_format"] == {"type": "json_object"}

    def test_a_plain_model_binds_tools_exactly_as_before(self):
        model = NormalizingChatModel(inner=_RecordingModel())

        bound = model.bind_tools([], tool_choice="auto")

        assert _RecordingModel.tool_bind_kwargs[-1] == {}
        assert bound.kwargs == {}
