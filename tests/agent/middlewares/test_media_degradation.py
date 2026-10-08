"""Silent-degradation detection: pure detector + LLMRetry success-path wiring.

The detector is the precision-first replacement for the original plan's
"reply never mentions the image" heuristic: only an explicit self-report of
media blindness (or an explicit request for the user to describe the media)
counts, so a capable model that simply describes an image without media
keywords is never misclassified.
"""

import asyncio

import pytest
from langchain.agents.middleware.types import ModelRequest
from langchain_core.messages import AIMessage, HumanMessage

from agent.middlewares import llm_capability_cache
from agent.middlewares.llm_retry.core import (
    FallbackCandidate,
    LLMRetryConfig,
    LLMRetryMiddleware,
    _extract_ai_text,
)
from agent.middlewares.media_pipeline.degradation import detect_media_blindness
from config.features import MEDIA_PIPELINE
from runtime import state_register_mem

pytestmark = [pytest.mark.unit, pytest.mark.timeout(60)]

SID = "sess-media-degradation"
TRYING_KEY = "_multimodal_trying_native"
MODEL_KEY = "_multimodal_native_model"
MAIN_MODEL_KEY = "testprov/testmodel"


class _SentinelModel:
    def __init__(self, name: str) -> None:
        self.model_name = name


def _request(model=_SentinelModel("main")) -> ModelRequest:
    return ModelRequest(
        model=model,  # type: ignore[arg-type]  # pyright: ignore[reportArgumentType]
        messages=[],
        state={"session_id": SID},
    )


def _media_request(model=_SentinelModel("main")) -> ModelRequest:
    return ModelRequest(
        model=model,  # type: ignore[arg-type]  # pyright: ignore[reportArgumentType]
        messages=[
            HumanMessage(
                content=[
                    {"type": "text", "text": "看图"},
                    {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}},
                ]
            )
        ],
        state={"session_id": SID},
    )


def _middleware(**config) -> LLMRetryMiddleware:
    return LLMRetryMiddleware(config=LLMRetryConfig(**config))


@pytest.fixture(autouse=True)
def _clean_session():
    yield
    state_register_mem.clear_session(SID)


@pytest.fixture(autouse=True)
def _clean_capability_cache():
    llm_capability_cache.reset_cache()
    yield
    llm_capability_cache.reset_cache()


def _arm_attempt(model_key: str = MAIN_MODEL_KEY) -> None:
    state_register_mem.set_state(SID, TRYING_KEY, True)
    state_register_mem.set_state(SID, MODEL_KEY, model_key)


# ---------------------------------------------------------------------------
# Detector — positive patterns (one class per language, plus describe requests)
# ---------------------------------------------------------------------------


class TestDetectorPositives:
    @pytest.mark.parametrize(
        "text",
        [
            "I cannot see the image you uploaded.",
            "I can't see the picture, sorry.",
            "I'm unable to view the photo.",
            "I cannot view the attached media.",
            "I cannot process images in this mode.",
            "As an AI, I have no vision to inspect the attachment.",
            "I don't have the ability to see images.",
        ],
    )
    def test_english_blindness_self_reports(self, text):
        assert detect_media_blindness(text) is True

    @pytest.mark.parametrize(
        "text",
        [
            "我无法查看图片",
            "抱歉，我这边看不到图",
            "我看不到图片内容",
            "我没有视觉能力，无法识别图片",
            "我无法访问你上传的图片",
            "模型没有视觉，只能走文字流程",
        ],
    )
    def test_chinese_blindness_self_reports(self, text):
        assert detect_media_blindness(text) is True

    @pytest.mark.parametrize(
        "text",
        [
            "このモデルは画像を見ることができません",
            "私は画像を認識できません",
            "画像が見えません",
            "私には視覚がないため対応できません",
        ],
    )
    def test_japanese_blindness_self_reports(self, text):
        assert detect_media_blindness(text) is True

    @pytest.mark.parametrize(
        "text",
        [
            "저는 이미지를 볼 수 없습니다",
            "이미지를 인식할 수 없어요",
            "시각이 없어서 처리하지 못합니다",
        ],
    )
    def test_korean_blindness_self_reports(self, text):
        assert detect_media_blindness(text) is True

    @pytest.mark.parametrize(
        "text",
        [
            "Could you please describe the image you sent?",
            "请描述一下图片内容",
            "画像を説明してくれませんか",
            "이미지를 설명해 주시겠어요?",
        ],
    )
    def test_describe_media_requests(self, text):
        assert detect_media_blindness(text) is True


class TestDetectorNegatives:
    @pytest.mark.parametrize(
        "text",
        [
            "The image shows a cat.",
            "The cat is orange and sits on the sofa.",
            "I can see the image clearly — it is a cat.",
            "这是一只橘猫。",
            "图片里有一只猫。",
            "画像には猫がいます。",
            "画像を見ることができます。",
            "画像が見えます。",
            "이미지에 고양이가 있습니다.",
            "你好，今天天气不错！",
            "Could you explain the plan again?",
            "I cannot see why this is a problem.",
            "",
        ],
    )
    def test_capable_or_unrelated_replies_do_not_match(self, text):
        assert detect_media_blindness(text) is False


# ---------------------------------------------------------------------------
# Integration — LLMRetry success path
# ---------------------------------------------------------------------------


class TestSilentDegradationOnSuccess:
    def test_blind_reply_caches_present_family_and_clears_flag(self):
        mw = _middleware(max_retries=0)
        _arm_attempt()

        result = mw.wrap_model_call(
            _media_request(),
            lambda req: AIMessage(content="I cannot see the image you uploaded."),
        )

        assert result.content == "I cannot see the image you uploaded."
        assert (
            llm_capability_cache.get_capability("testprov", "testmodel", "vision") == "unsupported"
        )
        assert state_register_mem.get_state(SID, TRYING_KEY, False) is False

    def test_normal_reply_keeps_cache_auto_and_clears_flag(self):
        mw = _middleware(max_retries=0)
        _arm_attempt()

        mw.wrap_model_call(
            _media_request(),
            lambda req: AIMessage(content="The image shows an orange cat on a sofa."),
        )

        assert llm_capability_cache.get_capability("testprov", "testmodel", "vision") == "auto"
        assert state_register_mem.get_state(SID, TRYING_KEY, False) is False

    def test_switch_off_skips_cache_but_still_clears_flag(self, monkeypatch):
        monkeypatch.setitem(MEDIA_PIPELINE, "main_llm_silent_degradation_detection", False)
        mw = _middleware(max_retries=0)
        _arm_attempt()

        mw.wrap_model_call(
            _media_request(),
            lambda req: AIMessage(content="I cannot see the image you uploaded."),
        )

        assert llm_capability_cache.get_capability("testprov", "testmodel", "vision") == "auto"
        assert state_register_mem.get_state(SID, TRYING_KEY, False) is False

    def test_without_native_attempt_no_cache_write(self):
        mw = _middleware(max_retries=0)

        mw.wrap_model_call(
            _media_request(),
            lambda req: AIMessage(content="I cannot see the image you uploaded."),
        )

        assert llm_capability_cache.get_capability("testprov", "testmodel", "vision") == "auto"

    def test_every_present_family_is_cached(self):
        mw = _middleware(max_retries=0)
        _arm_attempt()
        request = ModelRequest(
            model=_SentinelModel("main"),  # type: ignore[arg-type]  # pyright: ignore[reportArgumentType]
            messages=[
                HumanMessage(
                    content=[
                        {"type": "text", "text": "混合"},
                        {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}},
                        {"type": "video_bytes", "video_bytes": b"\x00\x00\x00\x18ftypmp42"},
                    ]
                )
            ],
            state={"session_id": SID},
        )

        mw.wrap_model_call(request, lambda req: AIMessage(content="我无法查看图片和视频"))

        assert (
            llm_capability_cache.get_capability("testprov", "testmodel", "vision") == "unsupported"
        )
        assert (
            llm_capability_cache.get_capability("testprov", "testmodel", "video") == "unsupported"
        )
        assert llm_capability_cache.get_capability("testprov", "testmodel", "audio") == "auto"

    def test_blind_text_in_multimodal_content_list_is_extracted(self):
        mw = _middleware(max_retries=0)
        _arm_attempt()
        result = AIMessage(
            content=[{"type": "text", "text": "抱歉，我无法查看图片"}],
        )

        mw.wrap_model_call(_media_request(), lambda req: result)

        assert (
            llm_capability_cache.get_capability("testprov", "testmodel", "vision") == "unsupported"
        )

    def test_async_blind_reply_caches(self):
        mw = _middleware(max_retries=0)
        _arm_attempt()

        async def handler(req):
            return AIMessage(content="I cannot see the image you uploaded.")

        asyncio.run(mw.awrap_model_call(_media_request(), handler))

        assert (
            llm_capability_cache.get_capability("testprov", "testmodel", "vision") == "unsupported"
        )
        assert state_register_mem.get_state(SID, TRYING_KEY, False) is False

    def test_detection_is_attributed_to_sticky_fallback_candidate(self):
        chain = [FallbackCandidate("deepseek", "deepseek-chat", _SentinelModel("fb1"))]
        mw = LLMRetryMiddleware(config=LLMRetryConfig(max_retries=0), fallback_chain=chain)
        state_register_mem.set_state(SID, "llm_fallback_index", 1)
        _arm_attempt()

        mw.wrap_model_call(
            _media_request(),
            lambda req: AIMessage(content="I cannot see the image you uploaded."),
        )

        assert (
            llm_capability_cache.get_capability("deepseek", "deepseek-chat", "vision")
            == "unsupported"
        )
        assert llm_capability_cache.get_capability("testprov", "testmodel", "vision") == "auto"
        assert state_register_mem.get_state(SID, MODEL_KEY, "") == "deepseek/deepseek-chat"


class TestExtractAiText:
    def test_bare_aimessage(self):
        assert _extract_ai_text(AIMessage(content="hello")) == "hello"

    def test_response_object_with_messages(self):
        class _Response:
            def __init__(self):
                self.messages = [AIMessage(content="wrapped")]

        assert _extract_ai_text(_Response()) == "wrapped"

    def test_non_ai_result_returns_empty(self):
        assert _extract_ai_text("plain string") == ""
