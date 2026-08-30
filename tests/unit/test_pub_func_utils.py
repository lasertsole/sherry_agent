"""Unit tests for pure-logic helpers in pub_func/.

Covers:
- pub_func/cjk.py                       (contains_cjk, is_cjk_codepoint, count_cjk)
- pub_func/string_to_int.py             (string_to_int, string_to_unique_int, rand_str_to_int)
- pub_func/generate_tsid.py             (generate_tsid)
- pub_func/process_sse_data.py          (process_sse_data)
- pub_func/extract_text_from_content.py (extract_text_from_content)
"""

import pytest
from types import SimpleNamespace

from pub_func.cjk import contains_cjk, is_cjk_codepoint, count_cjk
from pub_func.string_to_int import string_to_int, string_to_unique_int, rand_str_to_int
from pub_func.generate_tsid import generate_tsid
from pub_func.process_sse_data import process_sse_data
from pub_func.extract_text_from_content import extract_text_from_content


# --- cjk ---

class TestCjk:
    def test_contains_han_returns_true(self):
        assert contains_cjk("hello 世界") is True

    def test_contains_hiragana(self):
        assert contains_cjk("こんにちは") is True

    def test_contains_katakana(self):
        assert contains_cjk("カタカナ") is True

    def test_contains_hangul(self):
        assert contains_cjk("안녕") is True

    def test_ascii_only_returns_false(self):
        assert contains_cjk("hello world 123") is False

    def test_empty_returns_false(self):
        assert contains_cjk("") is False

    def test_is_cjk_codepoint_han(self):
        assert is_cjk_codepoint(ord("世")) is True

    def test_is_cjk_codepoint_ascii(self):
        assert is_cjk_codepoint(ord("a")) is False

    def test_count_cjk_mixed(self):
        assert count_cjk("ab世界cdこ") == 3

    def test_count_cjk_none(self):
        assert count_cjk("no cjk here") == 0


# --- string_to_unique_int ---

class TestStringToUniqueInt:
    def test_deterministic(self):
        assert string_to_unique_int("abc") == string_to_unique_int("abc")

    def test_different_inputs_differ(self):
        assert string_to_unique_int("abc") != string_to_unique_int("abd")

    def test_python_int_range(self):
        assert isinstance(string_to_unique_int("hello"), int)

    def test_returns_64bit_value(self):
        # first 8 bytes of SHA-256 digest as a big-endian int
        val = string_to_unique_int("hello")
        assert 0 <= val <= (1 << 64) - 1

    def test_empty_string_works(self):
        # SHA-256 of "" is well-defined
        assert string_to_unique_int("") >= 0


# --- rand_str_to_int ---

class TestRandStrToInt:
    def test_deterministic(self):
        assert rand_str_to_int("abc") == rand_str_to_int("abc")

    def test_different_inputs_differ(self):
        assert rand_str_to_int("abc") != rand_str_to_int("abd")

    def test_default_slice_len_is_8(self):
        val = rand_str_to_int("hello")
        assert 0 <= val <= 0xFFFFFFFF  # 8 hex chars -> 32-bit range

    def test_custom_slice_len(self):
        val = rand_str_to_int("hello", slice_len=4)
        assert 0 <= val <= 0xFFFF

    def test_slice_len_zero_raises(self):
        # int("", 16) has no valid literal
        with pytest.raises(ValueError):
            _ = rand_str_to_int("hello", slice_len=0)

    def test_returns_int(self):
        assert isinstance(rand_str_to_int("x"), int)


# --- string_to_int (canonical dispatcher) ---

class TestStringToInt:
    def test_sha256_matches_legacy_string_to_unique_int(self):
        for s in ("", "abc", "hello", "中文"):
            assert string_to_int(s, algorithm="sha256") == string_to_unique_int(s)

    def test_md5_matches_legacy_rand_str_to_int(self):
        for s in ("", "abc", "hello", "中文"):
            for n in (1, 4, 8, 16):
                assert string_to_int(s, algorithm="md5", slice_len=n) == rand_str_to_int(s, slice_len=n)

    def test_unknown_algorithm_raises(self):
        with pytest.raises(ValueError):
            _ = string_to_int("abc", algorithm="crc32")

    # Regression guards: these outputs are persisted (channel session ids,
    # checkpointer thread ids) — the algorithms must never change.
    def test_pinned_sha256_values(self):
        assert string_to_unique_int("hello") == 3238736544897475342
        assert string_to_unique_int("abc") == 13436514500253700074

    def test_pinned_md5_values(self):
        assert rand_str_to_int("hello") == 1564557354
        assert rand_str_to_int("abc") == 2416005272


# --- generate_tsid ---

class TestGenerateTsid:
    def test_matches_format(self):
        tsid = generate_tsid()
        assert len(tsid) == 14
        assert tsid.isdigit()

    def test_days_offset_past(self):
        tsid = generate_tsid(days_offset=-1)
        assert len(tsid) == 14
        assert tsid.isdigit()

    def test_days_offset_future(self):
        tsid = generate_tsid(days_offset=1)
        assert len(tsid) == 14
        assert tsid.isdigit()

    def test_zero_pads_month_and_day(self):
        from datetime import datetime, timedelta
        now = datetime.now() + timedelta(days=0)
        expected_month = str(now.month).zfill(2)
        expected_day = str(now.day).zfill(2)
        tsid = generate_tsid()
        assert tsid[4:6] == expected_month
        assert tsid[6:8] == expected_day


# --- process_sse_data ---

class TestProcessSseData:
    def test_extracts_data_lines(self):
        payload = "data: hello\n\ndata: world\n"
        assert process_sse_data(payload) == "hello\nworld"

    def test_ignores_non_data_lines(self):
        payload = "event: msg\ndata: only this\n"
        assert process_sse_data(payload) == "only this"

    def test_strips_whitespace_after_colon(self):
        payload = "data:   padded  "
        assert process_sse_data(payload) == "padded"

    def test_bytes_input(self):
        payload = b"data: bytes-line\n"
        assert process_sse_data(payload) == "bytes-line"

    def test_empty_input(self):
        assert process_sse_data("") == ""

    def test_none_input(self):
        assert process_sse_data(None) == ""

    def test_no_data_lines(self):
        assert process_sse_data("event: msg\nid: 1\n") == ""


# --- extract_text_from_content ---

class TestExtractTextFromContent:
    def test_string_content(self):
        assert extract_text_from_content("plain text") == "plain text"

    def test_empty_string(self):
        assert extract_text_from_content("") == ""

    def test_list_with_text_item(self):
        content = [SimpleNamespace(type="text", text="the answer")]
        assert extract_text_from_content(content) == "the answer"

    def test_list_skips_until_text_item(self):
        content = [
            SimpleNamespace(type="image_url", text="ignored"),
            SimpleNamespace(type="text", text="the answer"),
        ]
        assert extract_text_from_content(content) == "the answer"

    def test_list_without_text_item(self):
        content = [SimpleNamespace(type="image_url", text="ignored")]
        assert extract_text_from_content(content) == ""

    def test_empty_list(self):
        assert extract_text_from_content([]) == ""

    def test_other_type(self):
        assert extract_text_from_content(12345) == ""
