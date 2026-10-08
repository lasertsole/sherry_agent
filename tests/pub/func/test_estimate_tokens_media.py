"""Unit tests for block-wise multimodal token estimation.

The old estimator JSON-serialized a whole content list and divided its length by
``chars_per_token``, so a 5 MB base64 image read as ~1.25M tokens and fired
compression on its own. These tests lock the per-block contract: text is text,
media is a fixed per-type cost, and no block's base64 ever reaches the text
estimator.

The "old implementation" figures are recomputed here independently from
``json.dumps`` so the regression proof does not depend on the code under test.
"""

import json

import pytest
from langchain_core.messages import AIMessage, HumanMessage

from config.features import TOKEN_ESTIMATION
from pub.func.estimate_tokens import (
    estimate_content_tokens,
    estimate_msg_tokens,
    estimate_text_tokens,
)

pytestmark = [pytest.mark.unit]

CHARS_PER_TOKEN = TOKEN_ESTIMATION["chars_per_token"]
IMAGE = TOKEN_ESTIMATION["tokens_per_image_block"]
AUDIO = TOKEN_ESTIMATION["tokens_per_audio_block"]
VIDEO = TOKEN_ESTIMATION["tokens_per_video_block"]
UNKNOWN = TOKEN_ESTIMATION["tokens_per_unknown_block"]

_MEGABYTE_OF_BASE64 = "A" * 5_000_000


def _old_estimate(content) -> int:
    """The pre-change estimator: total chars // CHARS_PER_TOKEN, JSON-serialized."""
    return len(json.dumps(content)) // CHARS_PER_TOKEN


class TestLargeBase64Media:
    def test_five_megabyte_image_costs_the_image_floor(self):
        content = [
            {
                "type": "image_url",
                "image_url": {"url": f"data:image/png;base64,{_MEGABYTE_OF_BASE64}"},
            }
        ]
        estimated = estimate_msg_tokens(AIMessage(content=content))
        assert estimated == IMAGE
        assert estimated < 1_000

    def test_old_estimator_would_have_reported_a_million_plus(self):
        content = [
            {
                "type": "image_url",
                "image_url": {"url": f"data:image/png;base64,{_MEGABYTE_OF_BASE64}"},
            }
        ]
        assert _old_estimate(content) > 1_000_000

    def test_each_image_block_is_charged_once(self):
        content = [
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAA"}},
            {"type": "image", "source": {"type": "base64", "data": "BBB"}},
        ]
        assert estimate_content_tokens(content) == 2 * IMAGE

    def test_nested_anthropic_source_data_is_recognised_as_image(self):
        content = [{"type": "image", "source": {"type": "base64", "data": _MEGABYTE_OF_BASE64}}]
        assert estimate_content_tokens(content) == IMAGE


class TestMediaFamilies:
    def test_audio_url_data_url_costs_the_audio_floor(self):
        content = [
            {
                "type": "audio_url",
                "audio_url": {"url": f"data:audio/wav;base64,{_MEGABYTE_OF_BASE64}"},
            }
        ]
        assert estimate_content_tokens(content) == AUDIO

    def test_audio_bytes_dict_payload_costs_the_audio_floor(self):
        content = [{"type": "audio_bytes", "audio_bytes": {"bytes": b"x" * 1000}}]
        assert estimate_content_tokens(content) == AUDIO

    def test_video_url_data_url_costs_the_video_floor(self):
        content = [
            {
                "type": "video_url",
                "video_url": {"url": f"data:video/mp4;base64,{_MEGABYTE_OF_BASE64}"},
            }
        ]
        assert estimate_content_tokens(content) == VIDEO

    def test_video_bytes_payload_costs_the_video_floor(self):
        content = [{"type": "video_bytes", "video_bytes": {"bytes": b"x" * 1000}}]
        assert estimate_content_tokens(content) == VIDEO

    def test_deepagents_base64_field_is_media(self):
        content = [{"type": "image_url", "base64": _MEGABYTE_OF_BASE64}]
        assert estimate_content_tokens(content) == IMAGE


class TestUnknownBlocks:
    def test_unknown_block_without_media_costs_the_unknown_floor(self):
        content = [{"type": "some_future_block", "payload": {"k": "v"}}]
        assert estimate_content_tokens(content) == UNKNOWN

    def test_unknown_block_hiding_an_image_data_url_is_media(self):
        content = [
            {"type": "some_future_block", "url": f"data:image/png;base64,{_MEGABYTE_OF_BASE64}"}
        ]
        assert estimate_content_tokens(content) == IMAGE

    def test_unknown_block_hiding_audio_data_url_is_audio(self):
        content = [
            {"type": "some_future_block", "url": f"data:audio/wav;base64,{_MEGABYTE_OF_BASE64}"}
        ]
        assert estimate_content_tokens(content) == AUDIO

    def test_non_dict_block_costs_the_unknown_floor(self):
        assert estimate_content_tokens([1234]) == UNKNOWN


class TestTextAndEdges:
    def test_str_content_unchanged(self):
        assert estimate_content_tokens("abcdefgh") == 2

    def test_none_content_is_zero(self):
        assert estimate_content_tokens(None) == 0

    def test_empty_list_is_zero(self):
        assert estimate_content_tokens([]) == 0

    def test_pure_text_list_equals_concatenated_text(self):
        content = [{"type": "text", "text": "hello"}, {"type": "text", "text": "world"}]
        assert estimate_content_tokens(content) == estimate_text_tokens("hello\nworld")

    def test_text_plus_image_sums_both_costs(self):
        content = [
            {"type": "text", "text": "look at this image"},
            {
                "type": "image_url",
                "image_url": {"url": f"data:image/png;base64,{_MEGABYTE_OF_BASE64}"},
            },
        ]
        expected = estimate_text_tokens("look at this image") + IMAGE
        assert estimate_content_tokens(content) == expected

    def test_cjk_text_block_uses_cjk_ratio(self):
        content = [{"type": "text", "text": "你好你好"}]
        assert estimate_content_tokens(content) == 4 // TOKEN_ESTIMATION["chars_per_token_cjk"]

    def test_string_block_is_treated_as_text(self):
        assert estimate_content_tokens(["abcdefgh"]) == 2


class TestMessageIntegration:
    def test_media_message_estimate_is_the_fixed_cost(self):
        msg = HumanMessage(
            content=[
                {
                    "type": "image_url",
                    "image_url": {"url": f"data:image/png;base64,{_MEGABYTE_OF_BASE64}"},
                }
            ]
        )
        assert estimate_msg_tokens(msg) == IMAGE

    def test_media_estimate_does_not_scale_with_base64_size(self):
        small = [{"type": "image_url", "image_url": {"url": "data:image/png;base64,AA"}}]
        large = [
            {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{'A' * 2_000_000}"}}
        ]
        assert estimate_content_tokens(small) == estimate_content_tokens(large)
