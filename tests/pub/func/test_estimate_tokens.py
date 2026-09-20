"""Unit tests for the CJK-aware three-tier token estimator.

Tiers under test:

- **T1** — API-reported ``usage_metadata`` (``extract_reported_tokens`` and
  the auto-extract branch of ``estimate_messages_tokens``);
- **T2** — CJK-aware heuristic (``estimate_text_tokens`` /
  ``estimate_msg_tokens``);
- **T3** — the legacy ``len // CHARS_PER_TOKEN`` formula, which is the T2
  degenerate case for pure-ASCII text (pinned here as the regression floor).
"""

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from config.features import TOKEN_ESTIMATION
from pub.func.estimate_tokens import (
    estimate_messages_tokens,
    estimate_msg_tokens,
    estimate_text_tokens,
    extract_reported_tokens,
)

pytestmark = [pytest.mark.unit]

CHARS_PER_TOKEN = TOKEN_ESTIMATION["chars_per_token"]
CHARS_PER_TOKEN_CJK = TOKEN_ESTIMATION["chars_per_token_cjk"]

_USAGE_500 = {"input_tokens": 500, "output_tokens": 5, "total_tokens": 505}


class TestEstimateTextTokens:
    def test_pure_cjk_uses_cjk_ratio(self):
        text = "你好你好你好你好你好"
        assert estimate_text_tokens(text) == len(text) // CHARS_PER_TOKEN_CJK

    def test_pure_ascii_degenerates_to_legacy_formula(self):
        text = "abcdefghijklmnopqrstuvwxyz" * 4
        assert estimate_text_tokens(text) == len(text) // CHARS_PER_TOKEN

    def test_mixed_text_separates_cjk_from_ascii(self):
        text = "你好世界abcd"  # 4 CJK + 4 ASCII
        assert estimate_text_tokens(text) == 4 // CHARS_PER_TOKEN_CJK + 4 // CHARS_PER_TOKEN

    def test_empty_text_is_zero(self):
        assert estimate_text_tokens("") == 0

    def test_cjk_estimate_exceeds_legacy_estimate(self):
        text = "你好世界"
        assert estimate_text_tokens(text) > len(text) // CHARS_PER_TOKEN


class TestCjkScriptRanges:
    @pytest.mark.parametrize(
        "text",
        [
            pytest.param("汉字估算文本", id="hanzi"),
            pytest.param("ひらがなのもじ", id="hiragana"),
            pytest.param("カタカナノモジ", id="katakana"),
            pytest.param("한글추정텍스트", id="hangul"),
        ],
    )
    def test_script_characters_use_cjk_ratio(self, text):
        assert estimate_text_tokens(text) == len(text) // CHARS_PER_TOKEN_CJK


class TestExtractReportedTokens:
    def test_input_tokens_returned_when_usage_present(self):
        messages = [
            HumanMessage(content="hi"),
            AIMessage(content="ok", usage_metadata=dict(_USAGE_500)),
        ]
        assert extract_reported_tokens(messages) == 500

    def test_total_tokens_fallback_when_input_tokens_missing(self):
        # input_tokens is required by the pydantic model — inject the
        # malformed dict via model_copy (no validation).
        msg = AIMessage(content="ok").model_copy(update={"usage_metadata": {"total_tokens": 4321}})
        assert extract_reported_tokens([msg]) == 4321

    def test_none_when_no_ai_message(self):
        assert extract_reported_tokens([HumanMessage(content="hi")]) is None

    def test_none_when_usage_metadata_absent(self):
        assert extract_reported_tokens([AIMessage(content="ok")]) is None

    def test_none_when_usage_values_are_not_positive(self):
        msg = AIMessage(content="ok").model_copy(
            update={"usage_metadata": {"input_tokens": 0, "total_tokens": 0}}
        )
        assert extract_reported_tokens([msg]) is None

    def test_none_when_usage_value_is_a_bool(self):
        msg = AIMessage(content="ok").model_copy(
            update={"usage_metadata": {"input_tokens": True, "total_tokens": False}}
        )
        assert extract_reported_tokens([msg]) is None

    def test_old_usage_ignored_when_a_newer_ai_message_has_none(self):
        old = AIMessage(content="old", usage_metadata=dict(_USAGE_500))
        newer = AIMessage(content="new")
        assert extract_reported_tokens([old, newer]) is None


class TestEstimateMessagesTokens:
    def test_auto_tier1_used_when_last_ai_carries_usage(self):
        messages = [
            HumanMessage(content="hi"),
            AIMessage(content="ok", usage_metadata=dict(_USAGE_500)),
        ]
        assert estimate_messages_tokens(messages) == 500

    def test_reported_zero_skips_tier1_and_uses_tier2(self):
        messages = [AIMessage(content="abcdefgh", usage_metadata=dict(_USAGE_500))]
        # 8 ASCII chars -> 8 // 4 == 2, ignoring the reported 500.
        assert estimate_messages_tokens(messages, reported_tokens=0) == 2

    def test_explicit_reported_value_wins(self):
        messages = [HumanMessage(content="ignored")]
        assert estimate_messages_tokens(messages, reported_tokens=8192) == 8192

    def test_tier2_used_when_no_usage_metadata(self):
        messages = [HumanMessage(content="abcdefgh")]
        assert estimate_messages_tokens(messages) == 2


class TestEstimateMsgTokens:
    def test_content_only_ascii(self):
        assert estimate_msg_tokens(HumanMessage(content="abcdefgh")) == 2

    def test_text_block_list_matches_the_same_string_content(self):
        # Guards the removed JSON-serialization semantics that counted base64 as text.
        assert estimate_msg_tokens(
            AIMessage(content=[{"type": "text", "text": "hi"}])
        ) == estimate_msg_tokens(AIMessage(content="hi"))

    def test_empty_content_counts_zero(self):
        assert estimate_msg_tokens(HumanMessage(content="")) == 0

    def test_tool_call_name_and_args_counted(self):
        msg = AIMessage(
            content="abcdefgh",
            tool_calls=[{"name": "read_file", "args": {"path": "a.py"}, "id": "c1"}],
        )
        # 8 + 9 ("read_file") + 16 (str of the args dict) chars -> 2 + 2 + 4.
        assert estimate_msg_tokens(msg) == 8

    def test_tool_call_id_counted(self):
        msg = ToolMessage(content="abcdefgh", tool_call_id="call_1234")
        # 8 + 9 chars -> 2 + 2.
        assert estimate_msg_tokens(msg) == 4
