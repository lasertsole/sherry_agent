"""Thinking-mode turns must send their reasoning back to the provider.

DeepSeek (and OpenAI-compatible reasoning gateways) return
``additional_kwargs["reasoning_content"]`` on an assistant turn and reject the
next request when that turn is replayed without it:
``400 ... reasoning_content must be passed back``. langchain's message
converter drops unknown additional_kwargs, so both reasoning client classes
inject it into the outgoing payload themselves.
"""

from __future__ import annotations

import pytest
from langchain_core.messages import AIMessage, HumanMessage

from models.LLMs.reasoning_openai import (
    ReasoningChatOpenAI,
    ReasoningDeepSeekChat,
    _inject_reasoning_content,
    build_reasoning_aware_chat_model,
)

pytestmark = [pytest.mark.unit]


def _ai(reasoning: str | None) -> AIMessage:
    kwargs = {"reasoning_content": reasoning} if reasoning is not None else {}
    return AIMessage(
        content="",
        tool_calls=[{"name": "read_file", "args": {"path": "a.py"}, "id": "call-1"}],
        additional_kwargs=kwargs,
    )


@pytest.mark.parametrize(
    "model",
    [
        ReasoningDeepSeekChat(
            model="deepseek-chat", api_key="test", base_url="https://example.invalid/v1"
        ),
        ReasoningChatOpenAI(model="glm-4.5", api_key="test", base_url="https://example.invalid/v1"),
    ],
    ids=["deepseek", "openai-compatible"],
)
def test_the_request_payload_carries_the_assistant_reasoning(model):
    payload = model._get_request_payload([HumanMessage(content="hi"), _ai("why")])

    assistant = payload["messages"][-1]
    assert assistant["role"] == "assistant"
    assert assistant["reasoning_content"] == "why"
    assert assistant["tool_calls"]


def test_no_reasoning_means_no_extra_field():
    model = ReasoningDeepSeekChat(
        model="deepseek-chat", api_key="test", base_url="https://example.invalid/v1"
    )

    payload = model._get_request_payload([HumanMessage(content="hi"), _ai(None)])

    assert "reasoning_content" not in payload["messages"][-1]


def test_a_system_message_does_not_shift_the_mapping():
    payload: dict = {
        "messages": [
            {"role": "system", "content": "rules"},
            {"role": "assistant", "content": "", "tool_calls": []},
        ]
    }

    class _Model:
        @staticmethod
        def _convert_input(_input):
            class _Input:
                @staticmethod
                def to_messages():
                    return [HumanMessage(content="hi"), _ai("why")]

            return _Input()

    _inject_reasoning_content(_Model(), [], payload)

    assert payload["messages"][0]["role"] == "system"
    assert payload["messages"][1]["reasoning_content"] == "why"


def test_the_factory_routes_deepseek_through_the_replay_client(monkeypatch):
    """Auxiliary/family builders share this helper; DeepSeek must never get
    the plain ChatDeepSeek client (it drops reasoning_content on replay)."""
    from models.LLMs import reasoning_openai

    captured: dict = {}

    class _FakeDeepSeek:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    monkeypatch.setattr(reasoning_openai, "ReasoningDeepSeekChat", _FakeDeepSeek)

    model = build_reasoning_aware_chat_model(
        {"model_provider": "deepseek", "model": "deepseek-chat", "api_key": "k"},
        provider="deepseek",
    )

    assert isinstance(model, _FakeDeepSeek)
    assert captured["model"] == "deepseek-chat"
    assert "model_provider" not in captured


def test_the_factory_routes_glm_through_the_streaming_reasoning_client(monkeypatch):
    """Auxiliary/family families can run GLM too; vanilla ChatOpenAI drops the
    streamed ``reasoning_content`` delta."""
    from models.LLMs import reasoning_openai

    captured: dict = {}

    class _FakeGlm:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    monkeypatch.setattr(reasoning_openai, "ReasoningChatOpenAI", _FakeGlm)

    model = build_reasoning_aware_chat_model(
        {"model_provider": "openai", "model": "glm-4.6", "api_key": "k"},
        provider="openai",
    )

    assert isinstance(model, _FakeGlm)
    assert captured["model"] == "glm-4.6"
    assert "model_provider" not in captured


def test_the_factory_reads_the_provider_from_the_config_when_omitted(monkeypatch):
    from models.LLMs import reasoning_openai

    captured: dict = {}

    class _FakeDeepSeek:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    monkeypatch.setattr(reasoning_openai, "ReasoningDeepSeekChat", _FakeDeepSeek)

    model = build_reasoning_aware_chat_model(
        {"model_provider": "deepseek", "model": "deepseek-reasoner"},
        provider=None,
    )

    assert isinstance(model, _FakeDeepSeek)
    assert captured["model"] == "deepseek-reasoner"
