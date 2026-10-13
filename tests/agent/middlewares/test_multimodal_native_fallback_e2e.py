"""Hermetic end-to-end: native-first attempt → model rejection → cache → skill path.

Drives the real agent graph (``create_agent`` + ``MultimodalProcessor`` +
``LLMRetryMiddleware``) with a deterministic stub chat model that rejects media
blocks the way a non-vision provider does. No network, no real LLM: the whole
chain (before_agent tri-state decision, classified error, capability cache
write, request rewrite, retry, cross-session reuse) runs in-process.
"""

from __future__ import annotations

import base64
import io
from typing import ClassVar

import pytest
from PIL import Image
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langgraph.checkpoint.memory import MemorySaver

from agent.middlewares import llm_capability_cache
from agent.middlewares.llm_retry import LLMRetryMiddleware
from agent.middlewares.llm_retry.core import LLMRetryConfig
from agent.middlewares.media_pipeline import core as mm_core
from agent.middlewares.media_pipeline import fallback as mm_fallback
from config.features import MEDIA_PIPELINE
from runtime import state_register_mem

pytestmark = [pytest.mark.integration, pytest.mark.timeout(120)]

PROVIDER = "e2e-provider"
MODEL = "e2e-model"
OTHER_PROVIDER = "e2e-other-provider"
TRYING_KEY = "_multimodal_trying_native"

_MEDIA_ITEM_TYPES = frozenset({"image_url", "audio_url", "audio_bytes", "video_url", "video_bytes"})


def _png_base64() -> str:
    buf = io.BytesIO()
    Image.new("RGB", (2, 2), color=(4, 5, 6)).save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode("utf-8")


def _media_content() -> list:
    return [
        {"type": "text", "text": "请描述这张图片"},
        {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{_png_base64()}"}},
    ]


def _message_has_media(messages) -> bool:
    return any(
        isinstance(getattr(message, "content", None), list)
        and any(
            isinstance(item, dict) and item.get("type") in _MEDIA_ITEM_TYPES
            for item in message.content
        )
        for message in messages
    )


class _MediaRejectingStubModel(BaseChatModel):
    """Rejects any model call carrying a media block; answers text-only calls."""

    media_attempts: ClassVar[int] = 0
    text_attempts: ClassVar[int] = 0
    media_seen_flags: ClassVar[list[bool]] = []

    @property
    def _llm_type(self) -> str:
        return "stub-media-rejecting"

    def _generate(self, messages, stop=None, run_manager=None, **kwargs) -> ChatResult:
        has_media = _message_has_media(messages)
        type(self).media_seen_flags.append(has_media)
        if has_media:
            type(self).media_attempts += 1
            raise type("BadRequestError", (Exception,), {"status_code": 400})(
                "unsupported content type: image_url input not supported"
            )
        type(self).text_attempts += 1
        return ChatResult(
            generations=[ChatGeneration(message=AIMessage(content="skill path reply"))]
        )


class _MediaAcceptingStubModel(BaseChatModel):
    """Accepts media blocks natively — the silent-success case (no cache write)."""

    media_attempts: ClassVar[int] = 0
    media_seen_flags: ClassVar[list[bool]] = []

    @property
    def _llm_type(self) -> str:
        return "stub-media-accepting"

    def _generate(self, messages, stop=None, run_manager=None, **kwargs) -> ChatResult:
        has_media = _message_has_media(messages)
        type(self).media_seen_flags.append(has_media)
        if has_media:
            type(self).media_attempts += 1
        return ChatResult(generations=[ChatGeneration(message=AIMessage(content="native reply"))])


def _build_graph(model: BaseChatModel | None = None):
    from langchain.agents import create_agent
    from langchain.agents.middleware import AgentState

    class HarnessState(AgentState):
        session_id: str

    return create_agent(
        model=model or _MediaRejectingStubModel(),
        state_schema=HarnessState,
        checkpointer=MemorySaver(),
        tools=[],
        middleware=[
            mm_core.MultimodalProcessor(),
            LLMRetryMiddleware(config=LLMRetryConfig(max_retries=0)),
        ],
    )


def _invoke(graph, session_id: str) -> str:
    out = graph.invoke(
        {"messages": [HumanMessage(content=_media_content())], "session_id": session_id},
        {"configurable": {"thread_id": session_id}},
    )
    return out["messages"][-1].content


@pytest.fixture(autouse=True)
def _isolated_env(tmp_path, monkeypatch):
    monkeypatch.setattr(mm_core, "SRC_DIR", tmp_path / "src")
    monkeypatch.setattr(mm_fallback, "SRC_DIR", tmp_path / "src")
    monkeypatch.setenv("MAIN_LLM_PROVIDER", PROVIDER)
    monkeypatch.setenv("MAIN_LLM_NAME", MODEL)
    llm_capability_cache.reset_cache()
    _MediaRejectingStubModel.media_attempts = 0
    _MediaRejectingStubModel.text_attempts = 0
    _MediaRejectingStubModel.media_seen_flags = []
    _MediaAcceptingStubModel.media_attempts = 0
    _MediaAcceptingStubModel.media_seen_flags = []
    yield
    llm_capability_cache.reset_cache()


@pytest.fixture()
def src_root(tmp_path):
    return tmp_path / "src"


def test_rejection_records_cache_and_next_session_skips_native(src_root):
    graph = _build_graph()

    assert _invoke(graph, "e2e-native-1") == "skill path reply"
    assert _MediaRejectingStubModel.media_attempts == 1
    assert _MediaRejectingStubModel.text_attempts == 1
    assert llm_capability_cache.get_capability(PROVIDER, MODEL, "vision") == "unsupported"
    assert state_register_mem.get_state("e2e-native-1", TRYING_KEY, False) is False
    assert len(list((src_root / "e2e-native-1" / "media").glob("*.png"))) == 1

    assert _invoke(graph, "e2e-native-2") == "skill path reply"
    assert _MediaRejectingStubModel.media_attempts == 1
    assert _MediaRejectingStubModel.text_attempts == 2
    assert _MediaRejectingStubModel.media_seen_flags == [True, False, False]


def test_cache_is_isolated_per_model_key_in_the_graph(monkeypatch):
    graph = _build_graph()

    assert _invoke(graph, "e2e-key-1") == "skill path reply"
    assert llm_capability_cache.get_capability(PROVIDER, MODEL, "vision") == "unsupported"

    monkeypatch.setenv("MAIN_LLM_PROVIDER", OTHER_PROVIDER)

    assert _invoke(graph, "e2e-key-2") == "skill path reply"
    assert llm_capability_cache.get_capability(OTHER_PROVIDER, MODEL, "vision") == "unsupported"
    assert llm_capability_cache.get_capability(PROVIDER, MODEL, "vision") == "unsupported"
    assert _MediaRejectingStubModel.media_attempts == 2


def test_explicit_true_override_never_falls_back(monkeypatch):
    graph = _build_graph()
    llm_capability_cache.set_capability(PROVIDER, MODEL, "vision", "unsupported")
    monkeypatch.setitem(MEDIA_PIPELINE, "main_llm_native_multimodal", "true")

    with pytest.raises(Exception, match="unsupported content type"):
        _invoke(graph, "e2e-forced-native")

    assert _MediaRejectingStubModel.media_attempts == 1
    assert _MediaRejectingStubModel.text_attempts == 0


def test_native_success_keeps_cache_auto_and_reuses_native_next_session(src_root):
    """A model that silently accepts (or ignores) the blocks writes no cache
    entry — the next session probes native again instead of degrading early."""
    graph = _build_graph(_MediaAcceptingStubModel())

    assert _invoke(graph, "e2e-native-ok-1") == "native reply"
    assert llm_capability_cache.get_capability(PROVIDER, MODEL, "vision") == "auto"

    assert _invoke(graph, "e2e-native-ok-2") == "native reply"
    assert _MediaAcceptingStubModel.media_seen_flags == [True, True]
    assert _MediaAcceptingStubModel.media_attempts == 2
    assert llm_capability_cache.get_capability(PROVIDER, MODEL, "vision") == "auto"
