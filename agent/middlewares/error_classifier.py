"""Error classifier with structured recovery hints.

Equivalent to hermes-agent's ``agent/error_classifier.py`` (simplified).

Classifies API errors into structured ``ClassifiedError`` objects with
``retryable`` and ``should_compress`` flags.  This prevents infinite loops
by correctly identifying non-retryable errors (auth, billing, format)
vs. retryable ones (timeout, overload, rate limit).

Used by :class:`RetryGuard` and can be used standalone for error routing.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any


class FailoverReason(str, Enum):
    AUTH_PERMANENT = "auth_permanent"
    AUTH = "auth"
    BILLING = "billing"
    FORMAT_ERROR = "format_error"
    CONTENT_POLICY_BLOCKED = "content_policy_blocked"
    CONTEXT_OVERFLOW = "context_overflow"
    PAYLOAD_TOO_LARGE = "payload_too_large"
    RATE_LIMIT = "rate_limit"
    TIMEOUT = "timeout"
    SERVER_ERROR = "server_error"
    OVERLOADED = "overloaded"
    SSL_TRANSIENT = "ssl_transient"
    UNKNOWN = "unknown"


_NON_RETRYABLE_REASONS: set[FailoverReason] = {
    FailoverReason.AUTH_PERMANENT,
    FailoverReason.BILLING,
    FailoverReason.FORMAT_ERROR,
    FailoverReason.CONTENT_POLICY_BLOCKED,
}

_COMPRESS_REASONS: set[FailoverReason] = {
    FailoverReason.CONTEXT_OVERFLOW,
    FailoverReason.PAYLOAD_TOO_LARGE,
}

_BILLING_PATTERNS: tuple[str, ...] = (
    "billing", "quota", "insufficient_quota", "payment_required",
    "credit", "plan_limit", "subscription", "exceeded your current quota",
    "check your plan and billing details",
)

_RATE_LIMIT_PATTERNS: tuple[str, ...] = (
    "rate_limit", "rate limit", "too many requests", "429",
    "requests per", "rps limit", "tpm limit", "tokens per minute",
)

_OVERLOADED_PATTERNS: tuple[str, ...] = (
    "overloaded", "capacity", "temporarily unavailable", "try again later",
    "server is busy", "high load", "load shedding",
)

_CONTEXT_OVERFLOW_PATTERNS: tuple[str, ...] = (
    "context_length_exceeded", "context window", "max context",
    "too many tokens", "reduce the length", "maximum context length",
    "token limit", "context length", "context length",
)

_CONTENT_POLICY_PATTERNS: tuple[str, ...] = (
    "content_policy", "content policy", "content filter", "safety",
    "blocked", "flagged", "moderation", "harmful",
)

_SSL_PATTERNS: tuple[str, ...] = (
    "ssl", "certificate verify failed", "handshake",
    "tlsv13", "wrong version number", "decryption failed",
)

_SERVER_DISCONNECT_PATTERNS: tuple[str, ...] = (
    "broken pipe", "errno 32", "remote protocol",
    "connection reset", "connection lost", "peer closed",
    "server disconnected",
)

_REQUEST_VALIDATION_PATTERNS: tuple[str, ...] = (
    "invalid_request_error", "invalid x-api-key",
    "model_not_found", "model not found",
)


@dataclass
class ClassifiedError:
    reason: FailoverReason
    retryable: bool
    should_compress: bool
    message: str
    status_code: int | None = None


def _extract_status_code(error: Exception) -> int | None:
    depth = 0
    obj: Any = error
    while obj is not None and depth < 5:
        code = getattr(obj, "status_code", None)
        if code is not None:
            try:
                return int(code)
            except (TypeError, ValueError):
                pass
        resp = getattr(obj, "response", None)
        if resp is not None:
            obj = resp
            depth += 1
            continue
        break
    return None


def classify_error(error: Exception) -> ClassifiedError:
    """Classify an API error into a structured ``ClassifiedError``.

    Parameters
    ----------
    error : Exception
        The exception raised by the API call.

    Returns
    -------
    ClassifiedError
        Structured classification with recovery hints.
    """
    msg = str(error).lower()
    status = _extract_status_code(error)

    if status in (401, 403):
        if any(p in msg for p in ("invalid api key", "invalid x-api-key", "model_not_found")):
            return ClassifiedError(
                reason=FailoverReason.AUTH_PERMANENT,
                retryable=False, should_compress=False,
                message=str(error), status_code=status,
            )
        return ClassifiedError(
            reason=FailoverReason.AUTH,
            retryable=True, should_compress=False,
            message=str(error), status_code=status,
        )

    if status == 402:
        return ClassifiedError(
            reason=FailoverReason.BILLING,
            retryable=False, should_compress=False,
            message=str(error), status_code=status,
        )

    if status == 429:
        if any(p in msg for p in _OVERLOADED_PATTERNS):
            return ClassifiedError(
                reason=FailoverReason.OVERLOADED,
                retryable=True, should_compress=False,
                message=str(error), status_code=status,
            )
        return ClassifiedError(
            reason=FailoverReason.RATE_LIMIT,
            retryable=True, should_compress=False,
            message=str(error), status_code=status,
        )

    if status == 451:
        return ClassifiedError(
            reason=FailoverReason.CONTENT_POLICY_BLOCKED,
            retryable=False, should_compress=False,
            message=str(error), status_code=status,
        )

    if status in (400, 422):
        if any(p in msg for p in _CONTEXT_OVERFLOW_PATTERNS):
            return ClassifiedError(
                reason=FailoverReason.CONTEXT_OVERFLOW,
                retryable=True, should_compress=True,
                message=str(error), status_code=status,
            )
        if any(p in msg for p in _CONTENT_POLICY_PATTERNS):
            return ClassifiedError(
                reason=FailoverReason.CONTENT_POLICY_BLOCKED,
                retryable=False, should_compress=False,
                message=str(error), status_code=status,
            )
        if any(p in msg for p in _REQUEST_VALIDATION_PATTERNS):
            return ClassifiedError(
                reason=FailoverReason.FORMAT_ERROR,
                retryable=False, should_compress=False,
                message=str(error), status_code=status,
            )
        return ClassifiedError(
            reason=FailoverReason.FORMAT_ERROR,
            retryable=False, should_compress=False,
            message=str(error), status_code=status,
        )

    if status in (500, 502, 503, 504):
        return ClassifiedError(
            reason=FailoverReason.SERVER_ERROR,
            retryable=True, should_compress=False,
            message=str(error), status_code=status,
        )

    if any(p in msg for p in ("timeout", "timed out", "deadline")):
        if any(p in msg for p in _SERVER_DISCONNECT_PATTERNS):
            return ClassifiedError(
                reason=FailoverReason.TIMEOUT,
                retryable=True, should_compress=False,
                message=str(error), status_code=status,
            )
        return ClassifiedError(
            reason=FailoverReason.TIMEOUT,
            retryable=True, should_compress=False,
            message=str(error), status_code=status,
        )

    if any(p in msg for p in _CONTEXT_OVERFLOW_PATTERNS):
        return ClassifiedError(
            reason=FailoverReason.CONTEXT_OVERFLOW,
            retryable=True, should_compress=True,
            message=str(error), status_code=status,
        )

    if any(p in msg for p in _CONTENT_POLICY_PATTERNS):
        return ClassifiedError(
            reason=FailoverReason.CONTENT_POLICY_BLOCKED,
            retryable=False, should_compress=False,
            message=str(error), status_code=status,
        )

    if any(p in msg for p in _RATE_LIMIT_PATTERNS):
        return ClassifiedError(
            reason=FailoverReason.RATE_LIMIT,
            retryable=True, should_compress=False,
            message=str(error), status_code=status,
        )

    if any(p in msg for p in _BILLING_PATTERNS):
        return ClassifiedError(
            reason=FailoverReason.BILLING,
            retryable=False, should_compress=False,
            message=str(error), status_code=status,
        )

    if any(p in msg for p in _SSL_PATTERNS):
        return ClassifiedError(
            reason=FailoverReason.SSL_TRANSIENT,
            retryable=True, should_compress=False,
            message=str(error), status_code=status,
        )

    if any(p in msg for p in _OVERLOADED_PATTERNS):
        return ClassifiedError(
            reason=FailoverReason.OVERLOADED,
            retryable=True, should_compress=False,
            message=str(error), status_code=status,
        )

    return ClassifiedError(
        reason=FailoverReason.UNKNOWN,
        retryable=True, should_compress=False,
        message=str(error), status_code=status,
    )
