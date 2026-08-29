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

import pytest
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, AIMessageChunk, BaseMessage
from langchain_core.outputs import ChatGeneration, ChatGenerationChunk, ChatResult
from typing import Any, Iterator, List, Optional

from models.LLMs.reasoning_normalizer import NormalizingChatModel


class _FakeStreamingModel(BaseChatModel):
    """Emits canned per-chunk reasoning DELTAS (DeepSeek/GLM native shape)."""

    deltas: List[dict[str, str]] = []

    @property
    def _llm_type(self) -> str:
        return "fake-streaming"

    def _generate(
        self,
        messages: List[BaseMessage],
        stop: Optional[List[str]] = None,
        run_manager: Any = None,
        **kwargs: Any,
    ) -> ChatResult:
        full = "".join(d["reasoning_content"] for d in self.deltas)
        msg = AIMessage(content="", additional_kwargs={"reasoning_content": full})
        return ChatResult(generations=[ChatGeneration(message=msg)])

    def _stream(
        self,
        messages: List[BaseMessage],
        stop: Optional[List[str]] = None,
        run_manager: Any = None,
        **kwargs: Any,
    ) -> Iterator[ChatGenerationChunk]:
        for d in self.deltas:
            chunk = AIMessageChunk(content="", additional_kwargs=dict(d))
            yield ChatGenerationChunk(message=chunk)


def _make_model(deltas: List[dict[str, str]]) -> NormalizingChatModel:
    return NormalizingChatModel(inner=_FakeStreamingModel(deltas=deltas))


def _sync_stream_chunks(model: NormalizingChatModel) -> list[AIMessageChunk]:
    config = {"callbacks": None}
    return [
        gen.message
        for gen in model._stream(messages=[], config=config)  # type: ignore[arg-type]
    ]


class TestChunkDeltaPassthrough:
    def test_chunks_carry_deltas_verbatim(self):
        model = _make_model([{"reasoning_content": "The"}, {"reasoning_content": " user"}, {"reasoning_content": " asks"}])
        chunks = _sync_stream_chunks(model)
        assert [c.additional_kwargs["reasoning_content"] for c in chunks] == ["The", " user", " asks"]

    def test_no_cumulative_accumulation(self):
        deltas = [{"reasoning_content": d} for d in ["a", "ab", "abc"]]
        model = _make_model(deltas)
        chunks = _sync_stream_chunks(model)
        # If the normalizer accumulated, later chunks would repeat earlier text.
        assert [c.additional_kwargs["reasoning_content"] for c in chunks] == ["a", "ab", "abc"]

    def test_alias_keys_folded_into_canonical(self):
        model = _make_model([{"reasoning": "d1"}, {"reasoning_text": "d2"}, {"reasoning_content": "d3"}])
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
