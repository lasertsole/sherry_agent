"""``ReasoningChatOpenAI`` — a ``BaseChatOpenAI`` subclass that lifts
``reasoning_content`` out of OpenAI-compatible streaming responses.

Why it exists
    ``langchain-openai`` (Chat Completions path) does **not** extract
    ``reasoning_content`` from assistant deltas — the key is dropped during
    delta → ``AIMessageChunk`` conversion (see ``_convert_delta_to_message_chunk``),
    so reasoning models behind OpenAI-compatible gateways (Zhipu bigmodel GLM,
    vLLM, siliconflow, ...) lose their chain-of-thought entirely.

    ``langchain_deepseek.ChatDeepSeek`` solves this for DeepSeek by overriding
    ``_convert_chunk_to_generation_chunk`` (streaming) and ``_create_chat_result``
    (non-streaming) to copy ``reasoning_content`` into
    ``additional_kwargs["reasoning_content"]``. This module applies the exact
    same pattern, provider-agnostic, for the generic ``openai`` provider used by
    ``main_llm.py`` (GLM via ``open.bigmodel.cn/api/paas/v4`` and friends).

    Downstream, ``models/LLMs/reasoning_normalizer.py`` folds these deltas
    cumulatively into the canonical ``reasoning_content`` key and
    ``server/service/messages.py`` streams them to the client as
    ``{"type": "reasoning"}`` chunks.
"""

from __future__ import annotations

import openai
from langchain_core.messages import AIMessageChunk
from langchain_core.outputs import ChatGenerationChunk, ChatResult
from langchain_openai.chat_models.base import BaseChatOpenAI


class ReasoningChatOpenAI(BaseChatOpenAI):
    """``BaseChatOpenAI`` + ``reasoning_content`` capture (DeepSeek-style).

    Mirrors ``langchain_deepseek.ChatDeepSeek``: on both the streaming and
    non-streaming paths, provider reasoning deltas are copied from the raw
    response into ``additional_kwargs["reasoning_content"]`` so the project's
    ``NormalizingChatModel`` and reasoning streaming pipeline can pick them up.
    Harmless no-op for models/gateways that return no ``reasoning_content``.
    """

    def _create_chat_result(
        self,
        response: dict | openai.BaseModel,
        generation_info: dict | None = None,
    ) -> ChatResult:
        rtn = super()._create_chat_result(response, generation_info)
        if not isinstance(response, openai.BaseModel):
            return rtn
        choices = getattr(response, "choices", None)
        if choices and hasattr(choices[0].message, "reasoning_content"):
            rtn.generations[0].message.additional_kwargs["reasoning_content"] = choices[
                0
            ].message.reasoning_content
        # Some gateways (e.g. OpenRouter) expose reasoning under ``model_extra``.
        elif choices and hasattr(choices[0].message, "model_extra"):
            model_extra = choices[0].message.model_extra
            if isinstance(model_extra, dict):
                reasoning = model_extra.get("reasoning") or model_extra.get("reasoning_content")
                if isinstance(reasoning, str) and reasoning:
                    rtn.generations[0].message.additional_kwargs["reasoning_content"] = reasoning
        return rtn

    def _convert_chunk_to_generation_chunk(
        self,
        chunk: dict,
        default_chunk_class: type,
        base_generation_info: dict | None,
    ) -> ChatGenerationChunk | None:
        generation_chunk = super()._convert_chunk_to_generation_chunk(
            chunk, default_chunk_class, base_generation_info
        )
        if (choices := chunk.get("choices")) and generation_chunk:
            delta = choices[0].get("delta", {}) if isinstance(choices[0], dict) else {}
            if isinstance(generation_chunk.message, AIMessageChunk):
                reasoning_content = delta.get("reasoning_content")
                if reasoning_content is None:
                    # Gateway variant exposing the alias key instead.
                    reasoning_content = delta.get("reasoning")
                if reasoning_content is not None:
                    generation_chunk.message.additional_kwargs["reasoning_content"] = (
                        reasoning_content
                    )
        return generation_chunk


__all__ = ["ReasoningChatOpenAI"]
