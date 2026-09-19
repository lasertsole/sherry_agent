"""P1-a: history image stripping runs only when an image_url block exists.

``MultimodalProcessor.before_agent`` walks history on every turn. The strip is
now preceded by a cheap ``image_url`` presence check, and the content is only
reassigned when stripping actually produces text — a text-only block list (the
common shape left behind by an earlier turn) is no longer rewritten.
"""

import base64
import io

import pytest
from PIL import Image
from langchain_core.messages import AIMessage, HumanMessage

from agent.middlewares import llm_capability_cache as cache
from agent.middlewares.media_pipeline import core as mm_mod
from agent.middlewares.media_pipeline import fallback as mm_fallback
from config.features import MEDIA_PIPELINE
from runtime import state_register_mem

pytestmark = [pytest.mark.unit, pytest.mark.timeout(60)]

TRYING_KEY = "_multimodal_trying_native"
MODEL_KEY = "_multimodal_native_model"


def _png_base64() -> str:
    buf = io.BytesIO()
    Image.new("RGB", (2, 2), color=(7, 8, 9)).save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode("utf-8")


@pytest.fixture(autouse=True)
def _clean_capability_cache():
    cache.reset_cache()
    yield
    cache.reset_cache()


@pytest.fixture(autouse=True)
def _model_identity(monkeypatch):
    monkeypatch.setenv("MAIN_LLM_PROVIDER", "test-provider")
    monkeypatch.setenv("MAIN_LLM_NAME", "test-model")


@pytest.fixture()
def src_dir(tmp_path, monkeypatch):
    path = tmp_path / "src"
    monkeypatch.setattr(mm_mod, "SRC_DIR", path)
    monkeypatch.setattr(mm_fallback, "SRC_DIR", path)
    return path


@pytest.fixture()
def processor(src_dir):
    return mm_mod.MultimodalProcessor()


def _state(messages: list, session_id: str = "p1a-session") -> dict:
    return {"session_id": session_id, "messages": messages}


def _image_content(text: str) -> list:
    return [
        {"type": "text", "text": text},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}},
    ]


def _valid_image_content(text: str) -> list:
    return [
        {"type": "text", "text": text},
        {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{_png_base64()}"}},
    ]


class TestHistoryImageStrip:
    def test_history_image_block_is_stripped(self, processor):
        old = HumanMessage(content=_image_content("旧的"))
        current = HumanMessage(content=[{"type": "text", "text": "新的"}])

        processor._before_agent_impl(_state([old, current]))

        assert old.content == "旧的"

    def test_second_run_leaves_stripped_message_untouched(self, processor):
        old = HumanMessage(content=_image_content("旧的"))
        processor._before_agent_impl(
            _state([old, HumanMessage(content=[{"type": "text", "text": "新的"}])])
        )
        stripped = old.content

        processor._before_agent_impl(
            _state([old, HumanMessage(content=[{"type": "text", "text": "第三轮"}])])
        )

        assert old.content is stripped

    def test_text_only_list_history_is_not_rewritten(self, processor):
        old = HumanMessage(content=[{"type": "text", "text": "只有文本"}])
        original_content = old.content

        processor._before_agent_impl(
            _state([old, HumanMessage(content=[{"type": "text", "text": "新的"}])])
        )

        assert old.content is original_content
        assert isinstance(old.content, list)

    def test_image_only_history_keeps_its_content(self, processor):
        # Pre-existing semantics: without any text block the strip yields "",
        # so the content is left as-is (image included).
        old = HumanMessage(
            content=[{"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}}]
        )
        original_content = old.content

        processor._before_agent_impl(
            _state([old, HumanMessage(content=[{"type": "text", "text": "新的"}])])
        )

        assert old.content is original_content


# ---------------------------------------------------------------------------
# Smoke — pass-through and normalization
# ---------------------------------------------------------------------------


class TestSmokePassThrough:
    def test_plain_string_content_is_untouched(self, processor):
        mes = HumanMessage(content="纯文本")
        processor._before_agent_impl(_state([mes], "smoke-str"))
        assert mes.content == "纯文本"

    def test_last_message_not_human_is_ignored(self, processor):
        ai = AIMessage(content="我是 AI")
        processor._before_agent_impl(_state([ai], "smoke-ai"))
        assert ai.content == "我是 AI"

    def test_text_only_list_normalizes_to_single_text_dict(self, processor):
        mes = HumanMessage(content=[{"type": "text", "text": "hi"}, "not-a-dict"])
        processor._before_agent_impl(_state([mes], "smoke-text-list"))
        assert mes.content == [{"type": "text", "text": "hi"}]

    def test_empty_list_normalizes_to_empty_text_dict(self, processor):
        mes = HumanMessage(content=[])
        processor._before_agent_impl(_state([mes], "smoke-empty"))
        assert mes.content == [{"type": "text", "text": ""}]

    def test_two_text_items_still_raise(self, processor):
        mes = HumanMessage(content=[{"type": "text", "text": "a"}, {"type": "text", "text": "b"}])
        with pytest.raises(Exception, match="Only one text item allowed per input list"):
            processor._before_agent_impl(_state([mes], "smoke-two-texts"))


# ---------------------------------------------------------------------------
# Functional — three-state config: true / false / auto
# ---------------------------------------------------------------------------


class TestTriStateConfig:
    def test_true_keeps_media_blocks(self, processor, src_dir, monkeypatch):
        monkeypatch.setitem(MEDIA_PIPELINE, "main_llm_native_multimodal", "true")
        mes = HumanMessage(content=_valid_image_content("看图"))

        processor._before_agent_impl(_state([mes], "tri-true"))

        assert [item["type"] for item in mes.content] == ["text", "image_url"]
        assert len(mes.additional_kwargs["images"]) == 1
        assert (src_dir / "tri-true" / "media").exists()

    def test_true_keeps_history_media_blocks(self, processor, monkeypatch):
        monkeypatch.setitem(MEDIA_PIPELINE, "main_llm_native_multimodal", "true")
        old = HumanMessage(content=_valid_image_content("旧图"))
        current = HumanMessage(content=_valid_image_content("新图"))

        processor._before_agent_impl(_state([old, current], "tri-true-history"))

        assert isinstance(old.content, list)

    def test_false_takes_skill_path(self, processor, monkeypatch):
        monkeypatch.setitem(MEDIA_PIPELINE, "main_llm_native_multimodal", "false")
        old = HumanMessage(content=_valid_image_content("旧图"))
        current = HumanMessage(content=_valid_image_content("新图"))

        processor._before_agent_impl(_state([old, current], "tri-false"))

        assert [item["type"] for item in current.content] == ["text"]
        assert "image_to_text" in current.content[0]["text"]
        assert old.content == "旧图"
        assert len(current.additional_kwargs["images"]) == 1

    def test_auto_unprobed_keeps_blocks_and_marks_attempt(self, processor):
        mes = HumanMessage(content=_valid_image_content("看图"))

        processor._before_agent_impl(_state([mes], "auto-unprobed"))

        assert [item["type"] for item in mes.content] == ["text", "image_url"]
        assert state_register_mem.get_state("auto-unprobed", TRYING_KEY, False) is True
        assert (
            state_register_mem.get_state("auto-unprobed", MODEL_KEY, "")
            == "test-provider/test-model"
        )

    def test_auto_cached_supported_keeps_blocks_without_attempt_flag(self, processor):
        cache.set_capability("test-provider", "test-model", "vision", "supported")
        mes = HumanMessage(content=_valid_image_content("看图"))

        processor._before_agent_impl(_state([mes], "auto-supported"))

        assert [item["type"] for item in mes.content] == ["text", "image_url"]
        assert state_register_mem.get_state("auto-supported", TRYING_KEY, False) is False

    def test_auto_cached_unsupported_takes_skill_path(self, processor):
        cache.set_capability("test-provider", "test-model", "vision", "unsupported")
        mes = HumanMessage(content=_valid_image_content("看图"))

        processor._before_agent_impl(_state([mes], "auto-unsupported"))

        assert [item["type"] for item in mes.content] == ["text"]
        assert "image_to_text" in mes.content[0]["text"]

    def test_false_overrides_cached_supported(self, processor, monkeypatch):
        cache.set_capability("test-provider", "test-model", "vision", "supported")
        monkeypatch.setitem(MEDIA_PIPELINE, "main_llm_native_multimodal", "false")
        mes = HumanMessage(content=_valid_image_content("看图"))

        processor._before_agent_impl(_state([mes], "tri-false-over-cache"))

        assert [item["type"] for item in mes.content] == ["text"]
        assert "image_to_text" in mes.content[0]["text"]

    def test_auto_is_isolated_per_model_key(self, processor):
        cache.set_capability("other-provider", "test-model", "vision", "unsupported")
        mes = HumanMessage(content=_valid_image_content("看图"))

        processor._before_agent_impl(_state([mes], "auto-key-isolation"))

        assert [item["type"] for item in mes.content] == ["text", "image_url"]

    @pytest.mark.parametrize(
        ("illegal", "sid"),
        [
            ("", "illegal-empty"),
            ("yes", "illegal-yes"),
            ("TRUE", "illegal-upper"),
            ("1", "illegal-one"),
            ("on", "illegal-on"),
        ],
    )
    def test_illegal_value_fails_safe_to_skill_path(self, processor, illegal, sid, monkeypatch):
        monkeypatch.setitem(MEDIA_PIPELINE, "main_llm_native_multimodal", illegal)
        mes = HumanMessage(content=_valid_image_content("看图"))

        processor._before_agent_impl(_state([mes], sid))

        assert [item["type"] for item in mes.content] == ["text"]
        assert "image_to_text" in mes.content[0]["text"]
        assert len(mes.additional_kwargs["images"]) == 1
        assert state_register_mem.get_state(sid, TRYING_KEY, False) is False

    def test_illegal_value_overrides_cached_supported(self, processor, monkeypatch):
        cache.set_capability("test-provider", "test-model", "vision", "supported")
        monkeypatch.setitem(MEDIA_PIPELINE, "main_llm_native_multimodal", "")
        mes = HumanMessage(content=_valid_image_content("看图"))

        processor._before_agent_impl(_state([mes], "illegal-over-cache"))

        assert [item["type"] for item in mes.content] == ["text"]
        assert "image_to_text" in mes.content[0]["text"]

    def test_auto_clears_stale_flags_on_skill_path(self, processor):
        state_register_mem.set_state("auto-stale", TRYING_KEY, True)
        state_register_mem.set_state("auto-stale", MODEL_KEY, "stale/model")
        cache.set_capability("test-provider", "test-model", "vision", "unsupported")
        mes = HumanMessage(content=_valid_image_content("看图"))

        processor._before_agent_impl(_state([mes], "auto-stale"))

        assert state_register_mem.get_state("auto-stale", TRYING_KEY, False) is False
        assert state_register_mem.get_state("auto-stale", MODEL_KEY, "") == ""


# ---------------------------------------------------------------------------
# Functional — media families are judged independently
# ---------------------------------------------------------------------------


class TestIndependentMediaFamilies:
    @staticmethod
    def _video_item() -> dict:
        return {"type": "video_bytes", "video_bytes": b"\x00\x00\x00\x18ftypmp42"}

    @staticmethod
    def _audio_item() -> dict:
        return {"type": "audio_bytes", "audio_bytes": b"\x49\x44\x33\x03"}

    def test_video_unsupported_forces_skill_path_even_when_vision_supported(self, processor):
        cache.set_capability("test-provider", "test-model", "vision", "supported")
        cache.set_capability("test-provider", "test-model", "video", "unsupported")
        mes = HumanMessage(content=[*_valid_image_content("看图"), self._video_item()])

        processor._before_agent_impl(_state([mes], "mixed-video-off"))

        assert [item["type"] for item in mes.content] == ["text"]
        assert "image_to_text" in mes.content[0]["text"]
        assert "video_text_to_text" in mes.content[0]["text"]

    def test_unprobed_video_with_supported_vision_still_tries_native(self, processor):
        cache.set_capability("test-provider", "test-model", "vision", "supported")
        mes = HumanMessage(content=[*_valid_image_content("看图"), self._video_item()])

        processor._before_agent_impl(_state([mes], "mixed-video-auto"))

        assert [item["type"] for item in mes.content] == ["text", "image_url", "video_bytes"]
        assert state_register_mem.get_state("mixed-video-auto", TRYING_KEY, False) is True

    def test_audio_unsupported_with_unprobed_image_forces_skill_path(self, processor):
        cache.set_capability("test-provider", "test-model", "audio", "unsupported")
        mes = HumanMessage(content=[*_valid_image_content("看图"), self._audio_item()])

        processor._before_agent_impl(_state([mes], "mixed-audio-off"))

        assert [item["type"] for item in mes.content] == ["text"]
        assert state_register_mem.get_state("mixed-audio-off", TRYING_KEY, False) is False


# ---------------------------------------------------------------------------
# Functional — apply_skill_fallback (LLMRetry's rewrite entry point)
# ---------------------------------------------------------------------------


class TestApplySkillFallback:
    def test_strips_blocks_and_attaches_hints_without_mutating_input(self, processor):
        mes = HumanMessage(content=_valid_image_content("看图"))
        original_content = mes.content

        out = mm_fallback.apply_skill_fallback([mes], "fb-1")

        assert len(out) == 1 and out[0] is not mes
        assert [item["type"] for item in out[0].content] == ["text"]
        assert "image_to_text" in out[0].content[0]["text"]
        assert mes.content is original_content

    def test_reuses_recorded_paths_without_second_disk_write(self, processor, src_dir):
        recorded = "/persisted/media/20260101000000.png"
        mes = HumanMessage(content=_valid_image_content("看图"))
        mes.additional_kwargs = {"images": [recorded], "other": 1}

        out = mm_fallback.apply_skill_fallback([mes], "fb-reuse")

        assert recorded in out[0].content[0]["text"]
        assert out[0].additional_kwargs == mes.additional_kwargs
        assert not src_dir.exists()

    def test_processes_payload_when_no_path_was_recorded(self, processor, src_dir):
        mes = HumanMessage(content=_valid_image_content("看图"))

        out = mm_fallback.apply_skill_fallback([mes], "fb-fresh")

        assert (src_dir / "fb-fresh" / "media").exists()
        assert "image_to_text" in out[0].content[0]["text"]

    def test_non_human_last_message_returns_input(self, processor):
        messages = [HumanMessage(content="hi"), AIMessage(content="done")]

        assert mm_fallback.apply_skill_fallback(list(messages), "fb-ai") == messages

    def test_string_content_last_message_returns_input(self, processor):
        messages = [HumanMessage(content="纯文本")]

        assert mm_fallback.apply_skill_fallback(messages, "fb-str") is messages
