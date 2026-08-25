"""``NormalizingChatModel`` — a ``BaseChatModel`` wrapper that delegates to an
inner chat model while normalizing chain-of-thought (reasoning) output.

Why it exists
    ``main_llm.py`` / ``reasoner_llm.py`` / ``auxiliary_llm/core.py`` all build
    their model via ``langchain.chat_models.init_chat_model(...)`` and previously
    wrapped the result with ``.configurable_fields(temperature=ConfigurableField(id="temperature"))``.

    Reasoning models (e.g. DeepSeek's thinking mode, DeepSeek-R1 and Qwen
    reasoning variants) surface their chain-of-thought in provider-specific
    ``additional_kwargs`` keys — ``reasoning_content``, ``reasoning`` or
    ``reasoning_text`` — instead of inline ``content``. Downstream consumers
    (``server/service/messages.py``) read it exclusively from
    ``AIMessageChunk.additional_kwargs["reasoning_content"]`` and surface it as
    a ``{"type": "reasoning"}`` stream event.

    This wrapper collapses that provider variance into one canonical key so the
    rest of the codebase never has to special-case providers:

    * On full generation, any ``reasoning*`` key on the returned ``AIMessage``
      is normalized into ``additional_kwargs["reasoning_content"]``.
    * On streaming, ``reasoning*`` content from each ``AIMessageChunk`` is
      merged cumulatively into ``additional_kwargs["reasoning_content"]``.

It is a genuine ``BaseChatModel`` (so ``langchain.agents.create_agent`` and the
``.with_structured_output`` / ``.bind_tools`` mechanisms keep working) that
delegates ``_generate`` / ``_agenerate`` / ``_stream`` / ``_astream`` to the
inner model. Bound kwargs such as ``temperature`` pass straight through, so
``NormalizingChatModel(inner=model).bind(temperature=...)`` behaves exactly like
the ``configurable_fields(temperature=ConfigurableField(id="temperature"))``
call it replaced.
"""

from __future__ import annotations

from typing import Any, AsyncIterator, Iterator, List, Mapping, Optional, Sequence, Union

from langchain_core.callbacks import (
    AsyncCallbackManagerForLLMRun,
    CallbackManagerForLLMRun,
)
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, AIMessageChunk, BaseMessage
from langchain_core.outputs import ChatGeneration, ChatGenerationChunk, ChatResult
from pydantic import Field

_REASONING_KEYS = ("reasoning_content", "reasoning", "reasoning_text")


def _normalize_message_reasoning(message: BaseMessage) -> None:
    """Fold provider-specific reasoning keys into `reasoning_content`.

    Mutates ``message.additional_kwargs`` in place so the canonical key is
    present and stable. Existing ``reasoning_content`` wins over the aliases;
    aliases are then dropped to keep output predictable.
    """
    kwargs = dict(message.additional_kwargs or {})
    canonical = kwargs.get("reasoning_content")
    if not canonical:
        for key in _REASONING_KEYS[1:]:
            val = kwargs.get(key)
            if isinstance(val, str) and val:
                canonical = val
                break
    if isinstance(canonical, str) and canonical:
        kwargs["reasoning_content"] = canonical
    elif canonical is not None:
        kwargs["reasoning_content"] = str(canonical)
    for key in _REASONING_KEYS[1:]:
        kwargs.pop(key, None)
    message.additional_kwargs = kwargs


class NormalizingChatModel(BaseChatModel):
    """Delegate to an inner ``BaseChatModel``, normalizing reasoning output.

    Parameters
    ----------
    inner:
        The chat model to delegate generation to (e.g. the result of
        ``init_chat_model(...)`` or the local GGUF wrapper in
        ``auxiliary_llm``).
    """

    inner: BaseChatModel = Field(exclude=True)

    @property
    def _llm_type(self) -> str:
        return f"normalizing+{self.inner._llm_type}"

    @property
    def _identifying_params(self) -> Mapping[str, Any]:
        return {
            "inner_type": self.inner._llm_type,
            **dict(getattr(self.inner, "_identifying_params", {}) or {}),
        }

    @property
    def lc_attributes(self) -> Mapping[str, Any]:
        return self._identifying_params

    # -- Generation ---------------------------------------------------------
    def _generate(
        self,
        messages: List[BaseMessage],
        stop: Optional[List[str]] = None,
        run_manager: Optional[CallbackManagerForLLMRun] = None,
        **kwargs: Any,
    ) -> ChatResult:
        result = self.inner._generate(messages, stop=stop, run_manager=run_manager, **kwargs)
        for gen in result.generations:
            _normalize_message_reasoning(gen.message)
        return result

    async def _agenerate(
        self,
        messages: List[BaseMessage],
        stop: Optional[List[str]] = None,
        run_manager: Optional[AsyncCallbackManagerForLLMRun] = None,
        **kwargs: Any,
    ) -> ChatResult:
        result = await self.inner._agenerate(
            messages, stop=stop, run_manager=run_manager, **kwargs
        )
        for gen in result.generations:
            _normalize_message_reasoning(gen.message)
        return result

    # -- Streaming ----------------------------------------------------------
    def _stream(
        self,
        messages: List[BaseMessage],
        stop: Optional[List[str]] = None,
        run_manager: Optional[CallbackManagerForLLMRun] = None,
        **kwargs: Any,
    ) -> Iterator[ChatGenerationChunk]:
        accumulated: str = ""
        for chunk in self.inner._stream(
            messages, stop=stop, run_manager=run_manager, **kwargs
        ):
            accumulated = self._absorb_reasoning_chunk(chunk, accumulated)
            yield chunk

    async def _astream(
        self,
        messages: List[BaseMessage],
        stop: Optional[List[str]] = None,
        run_manager: Optional[AsyncCallbackManagerForLLMRun] = None,
        **kwargs: Any,
    ) -> AsyncIterator[ChatGenerationChunk]:
        accumulated: str = ""
        async for chunk in self.inner._astream(
            messages, stop=stop, run_manager=run_manager, **kwargs
        ):
            accumulated = self._absorb_reasoning_chunk(chunk, accumulated)
            yield chunk

    def _absorb_reasoning_chunk(
        self,
        chunk: ChatGenerationChunk,
        accumulated: str,
    ) -> str:
        """Merge a streamed reasoning delta into the canonical key.

        Reasoning providers stream chain-of-thought incrementally. Each chunk's
        ``reasoning*`` delta is appended to the running total, which is written
        to ``additional_kwargs["reasoning_content"]`` *in anticipation* — the
        final chunk thus carries the complete chain-of-thought for consumers
        that only inspect the last message.
        """
        msg = chunk.message
        kws = msg.additional_kwargs or {}
        delta = ""
        for key in _REASONING_KEYS:
            val = kws.get(key)
            if isinstance(val, str) and val:
                delta = val
                break
        if delta:
            accumulated += delta
            for key in _REASONING_KEYS:
                kws.pop(key, None)
            kws["reasoning_content"] = accumulated
            msg.additional_kwargs = kws
        return accumulated

    # -- Tool / structured-output delegation ---------------------------------
    def bind_tools(
        self,
        tools: Sequence[Union[type, Any]],
        *,
        tool_choice: Optional[Union[str, dict, bool]] = None,
        **kwargs: Any,
    ) -> Any:
        """Delegate tool binding to the inner model."""
        return self.inner.bind_tools(tools, tool_choice=tool_choice, **kwargs)

    def with_structured_output(
        self,
        schema: Union[type, dict[str, Any]],
        *,
        include_raw: bool = False,
        **kwargs: Any,
    ) -> Any:
        """Delegate structured output to the inner model."""
        return self.inner.with_structured_output(
            schema, include_raw=include_raw, **kwargs
        )

    # -- Message-parsing helpers (delegate to inner to keep behaviour aligned) --
    def _convert_input(self, input: Any) -> List[BaseMessage]:
        return self.inner._convert_input(input)

    def get_num_tokens(self, text: str) -> int:
        return self.inner.get_num_tokens(text)

    def get_num_tokens_from_messages(self, messages: List[BaseMessage]) -> int:
        return self.inner.get_num_tokens_from_messages(messages)


__all__ = ["NormalizingChatModel"]
