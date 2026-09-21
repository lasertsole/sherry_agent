"""ContentDecoder equivalence tests (DESIGN_PATTERN §3.1.7).

Pins the shared ``decode_content`` core and the four call sites that now
delegate to it: prefix-gated decode, malformed-JSON fallback vs strict
propagation, and the per-site post-processing. Each site's result on the same
inputs must stay field-for-field equal to the pre-refactor behavior.
"""

import json

import pytest

from context_engine.content_codec import decode_content
from context_engine.core import _CONTENT_JSON_PREFIX, _decode_content
from context_engine.embeddings.store import _decode
from context_engine.store.core import _decode_json_columns, _decode_title_content

pytestmark = [pytest.mark.unit, pytest.mark.timeout(60)]

_VALID = json.dumps({"parts": ["hello"]}, ensure_ascii=False)
_MALFORMED = "{not json"


class TestDecodeContentCore:
    def test_non_string_values_pass_through(self):
        marker = object()
        assert decode_content(marker) is marker
        assert decode_content(None) is None
        assert decode_content(3) == 3

    def test_valid_json_string_decodes(self):
        assert decode_content(_VALID) == {"parts": ["hello"]}
        assert decode_content("[1, 2]") == [1, 2]

    def test_malformed_returns_raw_by_default(self):
        assert decode_content(_MALFORMED) == _MALFORMED

    def test_malformed_raises_when_strict(self):
        with pytest.raises(json.JSONDecodeError):
            decode_content(_MALFORMED, strict=True)

    def test_prefix_gates_decoding(self):
        assert decode_content(_VALID, prefix=_CONTENT_JSON_PREFIX) == _VALID
        assert decode_content(_CONTENT_JSON_PREFIX + _VALID, prefix=_CONTENT_JSON_PREFIX) == {
            "parts": ["hello"]
        }

    def test_prefix_malformed_returns_raw(self):
        prefixed = _CONTENT_JSON_PREFIX + _MALFORMED
        assert decode_content(prefixed, prefix=_CONTENT_JSON_PREFIX) == prefixed


class TestDecodeContentSite:
    """context_engine/core.py::_decode_content keeps the marker + warning."""

    def test_prefixed_valid_decodes_plain_string_passes_through(self):
        assert _decode_content(_CONTENT_JSON_PREFIX + '"hello"') == "hello"
        # Without the marker a JSON-looking string is NOT decoded.
        assert _decode_content('"hello"') == '"hello"'
        assert _decode_content(None) is None

    def test_prefixed_malformed_returns_raw(self):
        raw = _CONTENT_JSON_PREFIX + _MALFORMED
        assert _decode_content(raw) is raw


class TestDecodeJsonColumnsSite:
    def _row(self, **overrides):
        row = {
            "content": _VALID,
            "tool_calls": "[1]",
            "images": None,
            "audios": '"a"',
            "videos": None,
            "ts_ms": 1,
            "idempotency_key": "k",
        }
        row.update(overrides)
        return row

    def test_decodes_string_cells_leaves_non_strings(self):
        row = _decode_json_columns(self._row())
        assert row["content"] == {"parts": ["hello"]}
        assert row["tool_calls"] == [1]
        assert row["images"] is None
        assert row["audios"] == "a"
        assert row["videos"] is None
        assert "ts_ms" not in row
        assert "idempotency_key" not in row

    def test_malformed_propagates(self):
        with pytest.raises(json.JSONDecodeError):
            _decode_json_columns(self._row(content=_MALFORMED))

    def test_matches_core_decoder_on_valid_payloads(self):
        row = _decode_json_columns(self._row())
        assert row["content"] == decode_content(_VALID, strict=True)


class TestDecodeTitleContentSite:
    def test_plain_json_string_is_trimmed(self):
        assert _decode_title_content(json.dumps("  hello  ")) == "hello"

    def test_multimodal_list_uses_first_text_part(self):
        raw = json.dumps([{"type": "image"}, {"type": "text", "text": "  caption  "}])
        assert _decode_title_content(raw) == "caption"

    def test_malformed_returns_raw_string(self):
        assert _decode_title_content(_MALFORMED) == _MALFORMED

    def test_empty_and_none_return_empty(self):
        assert _decode_title_content(None) == ""
        assert _decode_title_content("") == ""


class TestEmbeddingsDecodeSite:
    def test_non_string_unchanged(self):
        marker = object()
        assert _decode(marker) is marker
        assert _decode(None) is None

    def test_valid_decodes_malformed_returns_raw(self):
        assert _decode(_VALID) == {"parts": ["hello"]}
        assert _decode(_MALFORMED) == _MALFORMED

    def test_matches_shared_decoder(self):
        for value in (_VALID, _MALFORMED, "[1, 2]", "plain", None, 7):
            assert _decode(value) == decode_content(value)
