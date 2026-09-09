"""Unit tests for pub_func.message.llm_error_classifier (context-compression Task 2, TDD).

The classifier is consumed by the Summarization middleware retry loop
(agent/middlewares/summarization.py handler except-branches): its return value
routes the error to the payload_too_large (T4) or context_overflow (T5) retry
path, and None means "not a target error — caller must re-raise".

Dual-channel classification is mandatory: 20+ providers raise errors of wildly
varying shapes (some carry a status_code attribute, some only message strings),
so both channels are exercised here.
"""

import pytest

try:
    import httpx
    import openai
except ImportError:  # pragma: no cover - environment without provider SDKs
    httpx = None
    openai = None

from pub_func.message.llm_error_classifier import (
    CONTEXT_OVERFLOW,
    PAYLOAD_TOO_LARGE,
    ClassifiedError,
    FailoverReason,
    classify_api_error,
    classify_provider_error,
)


# ---------------------------------------------------------------------------
# Real-exception construction (openai + httpx when importable, stub fallback)
# ---------------------------------------------------------------------------


class _StubStatusError(Exception):
    """Fallback provider error carrying only a status_code attribute."""

    status_code: int | None = None

    def __init__(self, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


def _make_provider_error(message: str, status_code: int | None = None) -> Exception:
    """Build a real openai.BadRequestError when possible, stub otherwise."""
    if openai is not None and httpx is not None:
        response = httpx.Response(
            status_code=status_code if status_code is not None else 500,
            headers=httpx.Headers({"content-type": "application/json"}),
            request=httpx.Request("POST", "https://api.openai.com/v1/chat/completions"),
        )
        return openai.BadRequestError(message, response=response, body=None)
    return _StubStatusError(message, status_code=status_code)


# ---------------------------------------------------------------------------
# payload_too_large (HTTP 413) — status_code channel + string channel
# ---------------------------------------------------------------------------


pytestmark = [pytest.mark.unit]


def test_status_code_413_is_payload_too_large():
    exc = _make_provider_error("Payload Too Large", status_code=413)
    assert classify_provider_error(exc) == "payload_too_large"


def test_413_string_form_is_payload_too_large():
    exc = Exception("Error code: 413 - request payload too large for endpoint")
    assert classify_provider_error(exc) == "payload_too_large"


def test_openai_real_413_when_available():
    if openai is None or httpx is None:
        pytest.skip("openai/httpx not installed")
    exc = _make_provider_error("Payload Too Large", status_code=413)
    assert isinstance(exc, openai.BadRequestError)
    assert exc.status_code == 413
    assert classify_provider_error(exc) == "payload_too_large"


# ---------------------------------------------------------------------------
# context_overflow — each of the 7 fixed patterns (case-insensitive)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "pattern",
    [
        "context_length_exceeded",
        "This model's maximum context length is 65536 tokens",
        "The context length of the request exceeds the limit",
        "Your context window is too small for this request",
        "input length exceeds the model limit",
        "Please reduce the length of the messages",
        "too many tokens in the request",
    ],
)
def test_each_overflow_pattern(pattern: str):
    assert classify_provider_error(Exception(pattern)) == "context_overflow"


def test_overflow_pattern_uppercase_still_matches():
    assert classify_provider_error(Exception("CONTEXT LENGTH EXCEEDED")) == "context_overflow"


def test_overflow_found_through_cause_chain():
    inner = Exception("This model's maximum context length is 65536 tokens")
    outer = Exception("LLM call failed")
    outer.__cause__ = inner
    assert classify_provider_error(outer) == "context_overflow"


def test_overflow_found_at_depth_three_nesting():
    leaf = Exception("context_length_exceeded")
    mid = Exception("provider wrapper failed")
    mid.__cause__ = leaf
    outer = Exception("langchain run manager wrapper")
    outer.__cause__ = mid
    assert classify_provider_error(outer) == "context_overflow"


# ---------------------------------------------------------------------------
# Negatives — non-target errors return None (caller re-raises)
# ---------------------------------------------------------------------------


def test_timeout_error_is_not_classified():
    assert classify_provider_error(TimeoutError("timed out")) is None


def test_rate_limit_is_not_classified():
    assert classify_provider_error(Exception("rate limit exceeded")) is None


def test_401_403_strings_are_not_classified():
    assert classify_provider_error(Exception("401 Unauthorized")) is None
    assert classify_provider_error(Exception("403 Forbidden")) is None


def test_status_code_401_is_not_classified():
    exc = _make_provider_error("Unauthorized", status_code=401)
    assert classify_provider_error(exc) is None


def test_connection_error_is_not_classified():
    assert classify_provider_error(ConnectionError("connection reset by peer")) is None


# ---------------------------------------------------------------------------
# Input safety — never raises on odd input, returns None instead
# ---------------------------------------------------------------------------


def test_plain_object_returns_none_without_raising():
    # Runtime contract: non-exception input returns None instead of raising.
    assert classify_provider_error(object()) is None  # type: ignore[arg-type]  # pyright: ignore[reportArgumentType]


def test_none_returns_none_without_raising():
    assert classify_provider_error(None) is None  # type: ignore[arg-type]  # pyright: ignore[reportArgumentType]


# ---------------------------------------------------------------------------
# classify_api_error — full FailoverReason matrix (error-handling plan, A)
# ---------------------------------------------------------------------------


def _sdk_error(name: str, message: str, status_code: int | None = None) -> Exception:
    """Dynamically create an exception whose bare class name matches an SDK type."""
    cls = type(name, (Exception,), {"status_code": status_code})
    return cls(message)


class TestRecoveryMatrixEveryReason:
    """Every FailoverReason must be reachable with the action hints from the matrix."""

    def _assert(self, exc, reason, *, retryable, compress, fallback):
        classified = classify_api_error(exc)
        assert classified.reason is reason
        assert classified.retryable is retryable
        assert classified.should_compress is compress
        assert classified.should_fallback is fallback

    def test_auth_via_401_status(self):
        self._assert(
            _StubStatusError("Unauthorized", 401),
            FailoverReason.auth,
            retryable=True,
            compress=False,
            fallback=False,
        )

    def test_auth_via_403_status(self):
        self._assert(
            _StubStatusError("Forbidden", 403),
            FailoverReason.auth,
            retryable=True,
            compress=False,
            fallback=False,
        )

    def test_auth_permanent_via_sdk_type(self):
        self._assert(
            _sdk_error("PermissionDeniedError", "permission denied"),
            FailoverReason.auth_permanent,
            retryable=False,
            compress=False,
            fallback=True,
        )

    def test_billing_via_402_status(self):
        self._assert(
            _StubStatusError("Payment required", 402),
            FailoverReason.billing,
            retryable=False,
            compress=False,
            fallback=True,
        )

    def test_billing_via_message(self):
        self._assert(
            Exception("insufficient balance for this account"),
            FailoverReason.billing,
            retryable=False,
            compress=False,
            fallback=True,
        )

    def test_rate_limit_via_429_status(self):
        self._assert(
            _StubStatusError("Too many requests", 429),
            FailoverReason.rate_limit,
            retryable=True,
            compress=False,
            fallback=False,
        )

    def test_rate_limit_via_sdk_type(self):
        self._assert(
            _sdk_error("RateLimitError", "rate limit exceeded"),
            FailoverReason.rate_limit,
            retryable=True,
            compress=False,
            fallback=False,
        )

    def test_upstream_rate_limit_via_429_plus_message(self):
        self._assert(
            _StubStatusError("upstream error: provider unavailable", 429),
            FailoverReason.upstream_rate_limit,
            retryable=False,
            compress=False,
            fallback=True,
        )

    def test_overloaded_via_503_status(self):
        self._assert(
            _StubStatusError("Service unavailable", 503),
            FailoverReason.overloaded,
            retryable=True,
            compress=False,
            fallback=False,
        )

    def test_overloaded_via_529_status(self):
        self._assert(
            _StubStatusError("overloaded", 529),
            FailoverReason.overloaded,
            retryable=True,
            compress=False,
            fallback=False,
        )

    def test_server_error_via_500_status(self):
        self._assert(
            _StubStatusError("boom", 500),
            FailoverReason.server_error,
            retryable=True,
            compress=False,
            fallback=False,
        )

    def test_server_error_via_sdk_type(self):
        self._assert(
            _sdk_error("InternalServerError", "internal"),
            FailoverReason.server_error,
            retryable=True,
            compress=False,
            fallback=False,
        )

    def test_timeout_via_builtin_type(self):
        self._assert(
            TimeoutError("request timed out"),
            FailoverReason.timeout,
            retryable=True,
            compress=False,
            fallback=False,
        )

    def test_timeout_via_sdk_type(self):
        self._assert(
            _sdk_error("APITimeoutError", "Request timed out"),
            FailoverReason.timeout,
            retryable=True,
            compress=False,
            fallback=False,
        )

    def test_timeout_via_504_status(self):
        self._assert(
            _StubStatusError("Gateway timeout", 504),
            FailoverReason.timeout,
            retryable=True,
            compress=False,
            fallback=False,
        )

    def test_ssl_via_builtin_type(self):
        self._assert(
            Exception("ssl handshake failed"),
            FailoverReason.ssl_cert_verification,
            retryable=False,
            compress=False,
            fallback=True,
        )

    def test_ssl_via_warning_message(self):
        self._assert(
            Exception("certificate verify failed: self signed certificate"),
            FailoverReason.ssl_cert_verification,
            retryable=False,
            compress=False,
            fallback=True,
        )

    def test_context_overflow_via_message(self):
        self._assert(
            Exception("This model's maximum context length is 65536 tokens"),
            FailoverReason.context_overflow,
            retryable=False,
            compress=True,
            fallback=False,
        )

    def test_context_overflow_via_400_plus_message(self):
        self._assert(
            _StubStatusError("maximum context length exceeded", 400),
            FailoverReason.context_overflow,
            retryable=False,
            compress=True,
            fallback=False,
        )

    def test_context_overflow_via_token_budget(self):
        classified = classify_api_error(
            Exception("bad request"), approx_tokens=200000, context_length=200000
        )
        assert classified.reason is FailoverReason.context_overflow

    def test_payload_too_large_via_status(self):
        self._assert(
            _StubStatusError("Payload Too Large", 413),
            FailoverReason.payload_too_large,
            retryable=False,
            compress=True,
            fallback=False,
        )

    def test_payload_too_large_via_string(self):
        self._assert(
            Exception("Error code: 413 - request payload too large"),
            FailoverReason.payload_too_large,
            retryable=False,
            compress=True,
            fallback=False,
        )

    def test_image_too_large_via_message(self):
        self._assert(
            Exception("image too large: 8MB exceeds limit"),
            FailoverReason.image_too_large,
            retryable=True,
            compress=False,
            fallback=False,
        )

    def test_model_not_found_via_404_status(self):
        self._assert(
            _StubStatusError("not found", 404),
            FailoverReason.model_not_found,
            retryable=False,
            compress=False,
            fallback=True,
        )

    def test_model_not_found_via_sdk_type(self):
        self._assert(
            _sdk_error("NotFoundError", "The model does not exist"),
            FailoverReason.model_not_found,
            retryable=False,
            compress=False,
            fallback=True,
        )

    def test_provider_policy_blocked_via_message(self):
        self._assert(
            Exception("moderation_blocked by upstream"),
            FailoverReason.provider_policy_blocked,
            retryable=False,
            compress=False,
            fallback=True,
        )

    def test_content_policy_blocked_via_message(self):
        self._assert(
            Exception("content_policy_violation: prompt rejected"),
            FailoverReason.content_policy_blocked,
            retryable=False,
            compress=False,
            fallback=True,
        )

    def test_format_error_via_400_status(self):
        self._assert(
            _StubStatusError("bad json", 400),
            FailoverReason.format_error,
            retryable=False,
            compress=False,
            fallback=False,
        )

    def test_invalid_response_via_message(self):
        self._assert(
            Exception("provider returned an empty response"),
            FailoverReason.invalid_response,
            retryable=True,
            compress=False,
            fallback=False,
        )

    def test_unknown_fallback(self):
        self._assert(
            Exception("something entirely opaque happened"),
            FailoverReason.unknown,
            retryable=True,
            compress=False,
            fallback=False,
        )

    def test_real_httpx_remote_protocol_error_when_available(self):
        if httpx is None:
            pytest.skip("httpx not installed")
        exc = httpx.RemoteProtocolError(
            "peer closed connection without sending complete message body"
        )
        self._assert(
            exc, FailoverReason.server_error, retryable=True, compress=False, fallback=False
        )


class TestClassifiedErrorShape:
    def test_is_auth_property(self):
        auth = classify_api_error(_StubStatusError("401", 401))
        permanent = classify_api_error(_sdk_error("PermissionDeniedError", "denied"))
        other = classify_api_error(_StubStatusError("boom", 500))
        assert auth.is_auth and permanent.is_auth and not other.is_auth

    def test_metadata_fields_carried(self):
        classified = classify_api_error(
            _StubStatusError("boom", 500), provider="deepseek", model="deepseek-chat"
        )
        assert isinstance(classified, ClassifiedError)
        assert classified.provider == "deepseek"
        assert classified.model == "deepseek-chat"
        assert classified.status_code == 500
        assert classified.message == "boom"
        assert classified.error_context["exception_type"] == "_StubStatusError"

    def test_cause_chain_expanded(self):
        inner = Exception("This model's maximum context length is 65536 tokens")
        outer = Exception("LLM call failed")
        outer.__cause__ = inner
        assert classify_api_error(outer).reason is FailoverReason.context_overflow


class TestBackwardCompatConstants:
    def test_constant_values_unchanged(self):
        assert PAYLOAD_TOO_LARGE == "payload_too_large"
        assert CONTEXT_OVERFLOW == "context_overflow"

    def test_classify_provider_error_maps_only_overflow_pair(self):
        assert classify_provider_error(_StubStatusError("too large", 413)) == PAYLOAD_TOO_LARGE
        assert classify_provider_error(Exception("context window exceeded")) == CONTEXT_OVERFLOW
        assert classify_provider_error(TimeoutError("timed out")) is None
        assert classify_provider_error(_StubStatusError("boom", 500)) is None
