"""Tests for server.service.stream_diag (error-handling plan, module E)."""

from types import SimpleNamespace

import pytest

from server.service.stream_diag import (
    STREAM_DIAG_HEADERS,
    flatten_exception_chain,
    reraise_with_diag,
    stream_diag_capture_response,
    stream_diag_init,
    stream_diag_summary,
)

pytestmark = [pytest.mark.unit]


class TestStreamDiagInit:
    def test_initial_shape(self):
        diag = stream_diag_init()
        assert diag["chunks"] == 0
        assert diag["bytes"] == 0
        assert diag["first_chunk_at"] is None
        assert diag["headers"] == {}
        assert diag["http_status"] is None
        assert diag["started_at"] > 0


class TestCaptureResponse:
    def test_captures_status_and_known_headers(self):
        diag = stream_diag_init()

        class _FakeResponse:
            status_code = 503
            headers = {
                "CF-Ray": "abc123",
                "X-Request-Id": "req-9",
                "x-custom": "ignored",
            }

        stream_diag_capture_response(diag, _FakeResponse())
        assert diag["http_status"] == 503
        assert diag["headers"] == {"cf-ray": "abc123", "x-request-id": "req-9"}

    def test_only_stream_diag_headers_picked(self):
        diag = stream_diag_init()
        response = SimpleNamespace(headers=dict.fromkeys(STREAM_DIAG_HEADERS, "v"))
        stream_diag_capture_response(diag, response)
        assert diag["headers"] == dict.fromkeys(STREAM_DIAG_HEADERS, "v")

    def test_object_without_headers_is_ignored(self):
        diag = stream_diag_init()
        stream_diag_capture_response(diag, object())
        assert diag["http_status"] is None
        assert diag["headers"] == {}


class TestFlattenExceptionChain:
    def test_single_exception(self):
        text = flatten_exception_chain(ValueError("boom"))
        assert text == "ValueError: boom"

    def test_cause_chain_flattened(self):
        inner = ConnectionError("peer closed")
        outer = RuntimeError("stream died")
        outer.__cause__ = inner
        text = flatten_exception_chain(outer)
        assert text == "RuntimeError: stream died <- ConnectionError: peer closed"

    def test_context_chain_flattened(self):
        inner = TimeoutError("timed out")
        outer = RuntimeError("wrapper")
        outer.__context__ = inner
        text = flatten_exception_chain(outer)
        assert "RuntimeError: wrapper <- TimeoutError: timed out" == text

    def test_cycle_terminated(self):
        a = ValueError("a")
        b = TypeError("b")
        a.__cause__ = b
        b.__cause__ = a
        text = flatten_exception_chain(a)
        assert "<cycle>" in text


class TestStreamDiagSummary:
    def test_summary_without_chunks_or_error(self):
        diag = stream_diag_init()
        summary = stream_diag_summary(diag)
        assert "chunks=0" in summary
        assert "bytes=0" in summary
        assert "first_chunk_after=never" in summary
        assert "http_status=None" in summary
        assert "error=" not in summary

    def test_summary_with_error_and_counts(self):
        diag = stream_diag_init()
        diag["chunks"] = 7
        diag["bytes"] = 2048
        error = RuntimeError("died")
        summary = stream_diag_summary(diag, error)
        assert "chunks=7" in summary
        assert "bytes=2048" in summary
        assert "error=RuntimeError: died" in summary


class TestReraiseWithDiag:
    def test_message_appended_and_chained(self):
        original = ValueError("boom")

        def _raise():
            reraise_with_diag(original, "chunks=3 bytes=10")

        with pytest.raises(ValueError, match=r"boom \| diag: chunks=3 bytes=10") as info:
            _raise()
        assert info.value.__cause__ is original

    def test_multi_arg_constructor_reraised_untouched(self):
        class _MultiArgError(Exception):
            def __init__(self, a, b):  # noqa: A002
                super().__init__(f"{a}-{b}")

        original = _MultiArgError("x", "y")
        with pytest.raises(_MultiArgError) as info:
            reraise_with_diag(original, "summary")
        assert info.value is original
