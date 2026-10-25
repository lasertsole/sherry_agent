"""NON_OVERFLOW exclusions and the Cerebras bodyless case (plan §3 / §6)."""

from __future__ import annotations

import pytest

from pub.func.message.llm_error_classifier import FailoverReason, classify_api_error
from pub.func.message.llm_error_classifier import classify_provider_error

pytestmark = [pytest.mark.unit]


def test_bedrock_throttling_is_not_context_overflow():
    """P5: the exclusion only tightens; 'too many tokens' no longer misroutes it."""
    exc = Exception("ThrottlingException: Too many tokens, please wait")
    classified = classify_api_error(exc)
    assert classified.reason is FailoverReason.rate_limit
    # rate_limit is not a T4/T5 target, so the recovery loop re-raises.
    assert classify_provider_error(exc) is None


def test_rate_limit_wording_is_not_context_overflow():
    for message in (
        "rate limit exceeded while processing your request",
        "Too many requests; please retry later",
        "Service unavailable: rate limit",
    ):
        assert classify_provider_error(Exception(message)) is None


def test_the_exclusion_does_not_swallow_a_real_overflow():
    assert (
        classify_provider_error(Exception("Prompt is too long: 210000 tokens"))
        == "context_overflow"
    )
    assert classify_provider_error(Exception("context_length_exceeded")) == "context_overflow"


def test_a_throttling_error_with_a_429_status_stays_rate_limit():
    class _RateLimitedError(Exception):
        status_code = 429

    classified = classify_api_error(_RateLimitedError("Too many tokens; rate limit"))
    assert classified.reason is FailoverReason.rate_limit


def test_cerebras_bodyless_400_is_context_overflow():
    assert classifier_reason("400 (no body)") is FailoverReason.context_overflow
    assert classifier_reason("413 (no body)") is FailoverReason.context_overflow
    assert classifier_reason("400 status code (no body)") is FailoverReason.context_overflow


def test_a_plain_400_without_the_bodyless_marker_stays_format_error():
    class _BadRequestError(Exception):
        status_code = 400

    classified = classify_api_error(_BadRequestError("bad request"))
    assert classified.reason is FailoverReason.format_error


def classifier_reason(message: str) -> FailoverReason:
    return classify_api_error(Exception(message)).reason
