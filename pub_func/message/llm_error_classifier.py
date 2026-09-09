"""LLM API error classification engine.

Classifies a provider exception raised during an LLM call into a
:class:`FailoverReason` plus recovery-action hints (:class:`ClassifiedError`)
consumed by the retry middleware (agent/middlewares/llm_retry.py) and the
Summarization overflow-recovery loop (agent/middlewares/summarization.py).

Classification is a fixed priority pipeline over pattern tables (not
per-provider adapters) because 20+ providers in models/providers/registry.py
raise errors of wildly varying shapes: some carry a ``status_code`` attribute,
others only message strings, and LangChain may wrap the real cause. Hence the
dual-channel rules over BOTH the attribute and the string form, with the
string channel expanded through the ``__cause__`` / ``__context__`` chain up
to depth 3.

The classifier is READ-ONLY: it never raises on normal (and most abnormal)
input, never swallows an exception — it only classifies and returns. No
provider SDK is imported at module level (duck-typing only).
"""

# allow: SIZE_OK — most of the module is the fixed classification tables
# (status / type / message / recovery matrices) that must stay one unit.

import enum
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any

from loguru import logger


class FailoverReason(enum.Enum):
    """LLM API error reason — decides the recovery strategy."""

    # Auth / authorization
    auth = "auth"  # transient 401/403 — refreshable/rotatable
    auth_permanent = "auth_permanent"  # still 401/403 after refresh — unrecoverable

    # Billing / quota
    billing = "billing"  # 402 or confirmed exhausted quota — switch now
    rate_limit = "rate_limit"  # 429 or quota throttling — back off and retry
    upstream_rate_limit = "upstream_rate_limit"  # aggregator upstream 429 — switch model, not key

    # Server side
    overloaded = "overloaded"  # 503/529 — back off and retry
    server_error = "server_error"  # 500/502 — back off and retry

    # Transport
    timeout = "timeout"  # connect/read timeout — rebuild client + retry
    ssl_cert_verification = "ssl_cert_verification"  # TLS verification failed — fail fast

    # Context / payload
    context_overflow = "context_overflow"  # prompt exceeded the context window — compress
    payload_too_large = "payload_too_large"  # 413 — compress the payload
    image_too_large = "image_too_large"  # single image over the limit — shrink and retry

    # Model / provider policy
    model_not_found = "model_not_found"  # 404 — switch model
    provider_policy_blocked = "provider_policy_blocked"  # aggregator policy block
    content_policy_blocked = "content_policy_blocked"  # safety filter — never retry

    # Request format
    format_error = "format_error"  # 400 — terminate or trim and retry
    invalid_response = "invalid_response"  # empty/malformed response — retry or switch

    # Fallback
    unknown = "unknown"  # unclassifiable — back off and retry


@dataclass
class ClassifiedError:
    """Structured error classification + recovery-action hints."""

    reason: FailoverReason
    status_code: int | None = None
    provider: str | None = None
    model: str | None = None
    message: str = ""
    error_context: dict[str, Any] = field(default_factory=dict)

    retryable: bool = True
    should_compress: bool = False
    should_fallback: bool = False

    @property
    def is_auth(self) -> bool:
        return self.reason in {FailoverReason.auth, FailoverReason.auth_permanent}


PAYLOAD_TOO_LARGE = FailoverReason.payload_too_large.value
CONTEXT_OVERFLOW = FailoverReason.context_overflow.value

_STATUS_CODE_TOO_LARGE = 413
_PAYLOAD_TEXT_HINTS = ("payload", "too large")

_CONTEXT_OVERFLOW_PATTERNS = (
    "context_length_exceeded",
    "maximum context length",
    "context length",
    "context window",
    "input length exceeds",
    "reduce the length",
    "too many tokens",
)

_CAUSE_CHAIN_MAX_DEPTH = 3

_HTTP_STATUS_MAP: dict[int, FailoverReason] = {
    400: FailoverReason.format_error,
    401: FailoverReason.auth,
    402: FailoverReason.billing,
    403: FailoverReason.auth,
    404: FailoverReason.model_not_found,
    408: FailoverReason.timeout,
    413: FailoverReason.payload_too_large,
    429: FailoverReason.rate_limit,
    500: FailoverReason.server_error,
    502: FailoverReason.server_error,
    503: FailoverReason.overloaded,
    504: FailoverReason.timeout,
    529: FailoverReason.overloaded,
}

_EXCEPTION_TYPE_MAP: dict[str, FailoverReason] = {
    "TimeoutError": FailoverReason.timeout,
    "asyncio.TimeoutError": FailoverReason.timeout,
    "ConnectionError": FailoverReason.timeout,
    "ConnectionRefusedError": FailoverReason.timeout,
    "ConnectionResetError": FailoverReason.timeout,
    "ssl.SSLError": FailoverReason.ssl_cert_verification,
    "ssl.SSLCertVerificationError": FailoverReason.ssl_cert_verification,
    "openai.APITimeoutError": FailoverReason.timeout,
    "openai.APIConnectionError": FailoverReason.timeout,
    "openai.AuthenticationError": FailoverReason.auth,
    "openai.PermissionDeniedError": FailoverReason.auth_permanent,
    "openai.RateLimitError": FailoverReason.rate_limit,
    "openai.InternalServerError": FailoverReason.server_error,
    "openai.NotFoundError": FailoverReason.model_not_found,
    "httpx.ConnectTimeout": FailoverReason.timeout,
    "httpx.ReadTimeout": FailoverReason.timeout,
    "httpx.RemoteProtocolError": FailoverReason.server_error,
}

# Bare class names of the dotted (SDK-qualified) map keys. Real SDK classes
# live in private modules (openai._exceptions, httpx._exceptions), so the
# qualified lookup misses them and the bare name is the stable handle.
_SDK_TYPE_BY_BARE_NAME: dict[str, FailoverReason] = {
    key.split(".")[-1]: reason for key, reason in _EXCEPTION_TYPE_MAP.items() if "." in key
}

_MESSAGE_PATTERNS: dict[FailoverReason, tuple[str, ...]] = {
    FailoverReason.context_overflow: (
        "context_length_exceeded",
        "maximum context length",
        "context length",
        "context window",
        "input length exceeds",
        "reduce the length",
        "too many tokens",
    ),
    FailoverReason.content_policy_blocked: (
        "content_policy_violation",
        "content filter",
        "safety",
        "refusal",
        "content_filter",
    ),
    FailoverReason.overloaded: ("overload", "capacity", "server is overloaded"),
    FailoverReason.billing: (
        "billing",
        "payment",
        "insufficient balance",
        "quota exceeded",
        "credit",
    ),
    FailoverReason.rate_limit: ("rate limit", "too many requests", "throttling", "rate_limit"),
    FailoverReason.timeout: ("timeout", "timed out", "deadline exceeded", "etimedout"),
    FailoverReason.ssl_cert_verification: (
        "ssl",
        "certificate",
        "certificate_verify_failed",
        "cert verification",
    ),
    FailoverReason.model_not_found: ("model not found", "model not available", "unknown model"),
    FailoverReason.provider_policy_blocked: (
        "provider policy",
        "moderation_blocked",
        "flagged by moderation",
        "blocked by provider",
    ),
    FailoverReason.image_too_large: (
        "image too large",
        "image_too_large",
        "image exceeds",
        "image size exceeds",
    ),
    FailoverReason.invalid_response: (
        "invalid response",
        "empty response",
        "no response content",
        "response is empty",
    ),
}

# Step 5: TLS warning strings that appear in messages without a status code.
_SSL_TEXT_PATTERNS = (
    "certificate verify failed",
    "certificate_verify_failed",
    "sslv3",
    "tlsv1",
    "ssl handshake",
    "handshake failure",
)

# Step 6: server-side disconnect wording (mid-stream connection loss).
_SERVER_DISCONNECT_PATTERNS = (
    "server disconnected",
    "connection reset",
    "connection aborted",
    "peer closed connection",
    "remotedisconnected",
)

# Step 7: transport heuristics over class names of unknown provider SDKs.
_TRANSPORT_CLASS_HINTS: tuple[tuple[str, FailoverReason], ...] = (
    ("timeout", FailoverReason.timeout),
    ("connection", FailoverReason.timeout),
    ("ratelimit", FailoverReason.rate_limit),
    ("authentication", FailoverReason.auth),
    ("permission", FailoverReason.auth_permanent),
    ("notfound", FailoverReason.model_not_found),
    ("ssl", FailoverReason.ssl_cert_verification),
    ("certverification", FailoverReason.ssl_cert_verification),
)

_RECOVERY_MATRIX: dict[FailoverReason, dict[str, bool]] = {
    FailoverReason.auth: dict(retryable=True, should_compress=False, should_fallback=False),
    FailoverReason.auth_permanent: dict(
        retryable=False, should_compress=False, should_fallback=True
    ),
    FailoverReason.billing: dict(retryable=False, should_compress=False, should_fallback=True),
    FailoverReason.rate_limit: dict(retryable=True, should_compress=False, should_fallback=False),
    FailoverReason.upstream_rate_limit: dict(
        retryable=False, should_compress=False, should_fallback=True
    ),
    FailoverReason.overloaded: dict(retryable=True, should_compress=False, should_fallback=False),
    FailoverReason.server_error: dict(retryable=True, should_compress=False, should_fallback=False),
    FailoverReason.timeout: dict(retryable=True, should_compress=False, should_fallback=False),
    FailoverReason.ssl_cert_verification: dict(
        retryable=False, should_compress=False, should_fallback=True
    ),
    FailoverReason.context_overflow: dict(
        retryable=False, should_compress=True, should_fallback=False
    ),
    FailoverReason.payload_too_large: dict(
        retryable=False, should_compress=True, should_fallback=False
    ),
    FailoverReason.image_too_large: dict(
        retryable=True, should_compress=False, should_fallback=False
    ),
    FailoverReason.model_not_found: dict(
        retryable=False, should_compress=False, should_fallback=True
    ),
    FailoverReason.provider_policy_blocked: dict(
        retryable=False, should_compress=False, should_fallback=True
    ),
    FailoverReason.content_policy_blocked: dict(
        retryable=False, should_compress=False, should_fallback=True
    ),
    FailoverReason.format_error: dict(
        retryable=False, should_compress=False, should_fallback=False
    ),
    FailoverReason.invalid_response: dict(
        retryable=True, should_compress=False, should_fallback=False
    ),
    FailoverReason.unknown: dict(retryable=True, should_compress=False, should_fallback=False),
}


def classify_api_error(
    exc: BaseException,
    *,
    provider: str = "",
    model: str = "",
    approx_tokens: int = 0,
    context_length: int = 200000,
    num_messages: int = 0,
) -> ClassifiedError:
    """Classify a provider exception through the 8-step priority pipeline.

    Steps (first match wins):

    1. Special-case patterns (payload 413, context overflow, content policy,
       image size, token-budget overflow).
    2. HTTP status-code map (400 downgraded to context overflow when the
       message reports a context-window violation — providers return 400 for
       it and the Summarization T5 recovery depends on the classification).
    3. Exception type map (qualified or bare class name).
    4. Message pattern match.
    5. SSL/TLS warning patterns.
    6. Server-disconnect patterns.
    7. Transport heuristics over exception class names.
    8. Fallback to ``unknown``.

    Args:
        exc: The exception caught at the LLM call site.
        provider / model: Attribution metadata copied into the result.
        approx_tokens / context_length / num_messages: Call context; a token
            budget already at/over the context window forces
            ``context_overflow`` even without a provider pattern.
    """
    status_code = getattr(exc, "status_code", None)
    if not isinstance(status_code, int):
        status_code = None
    text = "\n".join(_exception_chain_texts(exc))
    lowered = text.lower()
    error_context: dict[str, Any] = {
        "exception_type": type(exc).__name__,
        "num_messages": num_messages,
    }

    reason = (
        _match_special_cases(status_code, lowered, approx_tokens, context_length)
        or _match_status_map(status_code, lowered)
        or _match_exception_type(exc)
        or _match_message_patterns(lowered)
        or _match_ssl_patterns(lowered)
        or _match_server_disconnect(lowered)
        or _match_transport_heuristics(exc)
        or FailoverReason.unknown
    )

    recovery = _RECOVERY_MATRIX[reason]
    logger.debug(
        "classify_api_error: {} -> {} (retryable={}, compress={}, fallback={})",
        type(exc).__name__,
        reason.value,
        recovery["retryable"],
        recovery["should_compress"],
        recovery["should_fallback"],
    )
    return ClassifiedError(
        reason=reason,
        status_code=status_code,
        provider=provider or None,
        model=model or None,
        message=text,
        error_context=error_context,
        retryable=recovery["retryable"],
        should_compress=recovery["should_compress"],
        should_fallback=recovery["should_fallback"],
    )


def classify_provider_error(exc: BaseException) -> str | None:
    """Backward-compatible wrapper for the Summarization T4/T5 loop.

    Args:
        exc: The exception caught at the LLM handler call site. Any other
            object (including ``None``) is treated as "not a target error".

    Returns:
        ``"payload_too_large"`` for HTTP 413-shaped errors,
        ``"context_overflow"`` for context-window-exceeded errors,
        ``None`` for everything else. ``None`` means the caller must
        re-raise the original exception untouched.
    """
    # Runtime-defensive: callers in except-branches may forward arbitrary
    # objects; anything that is not an exception classifies as non-target.
    if not isinstance(exc, BaseException):  # pyright: ignore[reportUnnecessaryIsInstance]
        logger.debug("classify_provider_error: non-exception input {!r} -> None", exc)  # pyright: ignore[reportUnreachable]
        return None

    classified = classify_api_error(exc)
    if classified.reason in (FailoverReason.payload_too_large, FailoverReason.context_overflow):
        return classified.reason.value
    return None


def _exception_chain_texts(exc: BaseException, depth: int = 0) -> Iterator[str]:
    """Yield the text of ``exc`` then its ``__cause__``/``__context__`` chain.

    Walks at most ``_CAUSE_CHAIN_MAX_DEPTH`` exceptions deep so LangChain's
    wrapper layers are expanded without unbounded recursion.
    """
    if depth >= _CAUSE_CHAIN_MAX_DEPTH:
        return
    text = str(exc).strip()
    yield text if text else repr(exc)
    for linked in (exc.__cause__, exc.__context__):
        if isinstance(linked, BaseException) and linked is not exc:
            yield from _exception_chain_texts(linked, depth + 1)


def _match_special_cases(
    status_code: int | None, lowered: str, approx_tokens: int, context_length: int
) -> FailoverReason | None:
    """Step 1: patterns that outrank the generic maps."""
    if _match_payload_too_large(status_code, lowered):
        return FailoverReason.payload_too_large
    if any(pattern in lowered for pattern in _CONTEXT_OVERFLOW_PATTERNS):
        return FailoverReason.context_overflow
    if approx_tokens > 0 and context_length > 0 and approx_tokens >= context_length:
        return FailoverReason.context_overflow
    if any(kw in lowered for kw in ("content_filter", "content_policy")):
        return FailoverReason.content_policy_blocked
    return None


def _match_payload_too_large(status_code: int | None, lowered: str) -> bool:
    """HTTP 413 via a status_code attribute, or a 413 payload/too-large string."""
    if status_code == _STATUS_CODE_TOO_LARGE:
        return True
    return "413" in lowered and any(hint in lowered for hint in _PAYLOAD_TEXT_HINTS)


def _match_status_map(status_code: int | None, lowered: str) -> FailoverReason | None:
    """Step 2: HTTP status map; aggregator upstream 429 gets its own reason."""
    if status_code is None:
        return None
    if status_code == 429 and "upstream" in lowered:
        return FailoverReason.upstream_rate_limit
    return _HTTP_STATUS_MAP.get(status_code)


def _match_exception_type(exc: BaseException) -> FailoverReason | None:
    """Step 3: exception type map — qualified name, then bare/SDK bare name."""
    for cls in type(exc).__mro__:
        qualified = f"{cls.__module__}.{cls.__qualname__}"
        if qualified in _EXCEPTION_TYPE_MAP:
            return _EXCEPTION_TYPE_MAP[qualified]
        bare = _SDK_TYPE_BY_BARE_NAME.get(cls.__name__)
        if bare is not None:
            return bare
        reason = _EXCEPTION_TYPE_MAP.get(cls.__name__)
        if reason is not None:
            return reason
    return None


def _match_message_patterns(lowered: str) -> FailoverReason | None:
    """Step 4: fixed message-pattern table (dict order = priority)."""
    for reason, patterns in _MESSAGE_PATTERNS.items():
        if any(pattern in lowered for pattern in patterns):
            return reason
    return None


def _match_ssl_patterns(lowered: str) -> FailoverReason | None:
    """Step 5: TLS warning strings without a status code or known type."""
    if any(pattern in lowered for pattern in _SSL_TEXT_PATTERNS):
        return FailoverReason.ssl_cert_verification
    return None


def _match_server_disconnect(lowered: str) -> FailoverReason | None:
    """Step 6: server-side disconnect wording (mid-stream connection loss)."""
    if any(pattern in lowered for pattern in _SERVER_DISCONNECT_PATTERNS):
        return FailoverReason.server_error
    return None


def _match_transport_heuristics(exc: BaseException) -> FailoverReason | None:
    """Step 7: class-name heuristics for unknown provider SDK exception types."""
    for cls in type(exc).__mro__:
        name = cls.__name__.lower().replace("_", "")
        for hint, reason in _TRANSPORT_CLASS_HINTS:
            if hint in name:
                return reason
    return None
