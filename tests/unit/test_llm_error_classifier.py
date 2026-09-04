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

from pub_func.message.llm_error_classifier import classify_provider_error


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
