"""Shared multimodal message conversion (DESIGN_PATTERN §5.2).

The text+image_url conversion used to be copied verbatim into ITTT and VTTT
(plus a video_url branch in VTTT). It now lives on
``LocalMultimodalLlamaChatBase`` with a ``_convert_content_block`` hook. These
tests pin the converted dicts field-for-field and prove the VTTT subclass only
diverges on the documented ``video_url`` branch.
"""

from typing import Any

import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

from models.LLMs.base_local_llama import LocalMultimodalLlamaChatBase

pytestmark = [pytest.mark.unit, pytest.mark.timeout(60)]

_VIDEO_PLACEHOLDER = "[video content — use extracted frames as image_url instead]"


class _ITTTLike(LocalMultimodalLlamaChatBase):
    def _resolve_model_path(self) -> str:
        return "/fake/model.gguf"

    def _resolve_mmproj_path(self) -> str:
        return "/fake/mmproj.bin"


class _VTTTLike(_ITTTLike):
    def _convert_content_block(self, block: dict[str, Any]) -> dict[str, Any]:
        if block.get("type") == "video_url":
            return {"type": "text", "text": _VIDEO_PLACEHOLDER}
        return super()._convert_content_block(block)


def _make(cls):
    return cls(model_path="/fake/model.gguf", mmproj_path="/fake/mmproj.bin")


class TestRolesAndPlainText:
    def test_roles(self):
        model = _make(_ITTTLike)
        assert model._convert_message_to_dict(HumanMessage(content="a")) == {
            "role": "user",
            "content": "a",
        }
        assert model._convert_message_to_dict(AIMessage(content="b")) == {
            "role": "assistant",
            "content": "b",
        }
        assert model._convert_message_to_dict(SystemMessage(content="c")) == {
            "role": "system",
            "content": "c",
        }

    def test_empty_content_stays_empty_string(self):
        model = _make(_ITTTLike)
        assert model._convert_message_to_dict(HumanMessage(content="")) == {
            "role": "user",
            "content": "",
        }


class TestMultimodalBlocks:
    def test_text_and_image_url_dict(self):
        model = _make(_ITTTLike)
        msg = HumanMessage(
            content=[
                {"type": "text", "text": "look"},
                {"type": "image_url", "image_url": {"url": "data:image/png;base64,AA"}},
            ]
        )
        assert model._convert_message_to_dict(msg) == {
            "role": "user",
            "content": [
                {"type": "text", "text": "look"},
                {"type": "image_url", "image_url": "data:image/png;base64,AA"},
            ],
        }

    def test_image_url_non_dict_is_passed_as_value(self):
        model = _make(_ITTTLike)
        msg = HumanMessage(content=[{"type": "image_url", "image_url": "plain-url"}])
        assert model._convert_message_to_dict(msg) == {
            "role": "user",
            "content": [{"type": "image_url", "image_url": "plain-url"}],
        }

    def test_unknown_blocks_and_non_dicts_pass_through(self):
        model = _make(_ITTTLike)
        unknown = {"type": "audio_bytes", "data": "x"}
        msg = HumanMessage(content=[unknown, "raw-block"])
        assert model._convert_message_to_dict(msg) == {
            "role": "user",
            "content": [unknown, "raw-block"],
        }


class TestVideoUrlHook:
    def test_ittt_like_passes_video_url_through(self):
        model = _make(_ITTTLike)
        msg = HumanMessage(content=[{"type": "video_url", "video_url": {"url": "v"}}])
        assert model._convert_message_to_dict(msg) == {
            "role": "user",
            "content": [{"type": "video_url", "video_url": {"url": "v"}}],
        }

    def test_vttt_like_replaces_video_url_with_placeholder(self):
        model = _make(_VTTTLike)
        msg = HumanMessage(content=[{"type": "video_url", "video_url": {"url": "v"}}])
        assert model._convert_message_to_dict(msg) == {
            "role": "user",
            "content": [{"type": "text", "text": _VIDEO_PLACEHOLDER}],
        }


@pytest.mark.parametrize(
    "content",
    [
        "plain text",
        [{"type": "text", "text": "a"}],
        [{"type": "image_url", "image_url": {"url": "u"}}],
        [{"type": "image_url", "image_url": "u"}],
        [{"type": "audio_bytes", "data": "x"}],
        [{"type": "text", "text": "a"}, "raw"],
    ],
)
def test_ittt_and_vttt_agree_on_non_video_blocks(content):
    ittt = _make(_ITTTLike)
    vttt = _make(_VTTTLike)
    assert ittt._convert_message_to_dict(
        HumanMessage(content=content)
    ) == vttt._convert_message_to_dict(HumanMessage(content=content))
