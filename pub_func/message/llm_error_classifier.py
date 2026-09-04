"""LLM provider error classifier for the context-compression retry loop.

Classifies a provider exception raised during an LLM call into one of two
retryable categories consumed by the Summarization middleware
(agent/middlewares/summarization.py handler except-branches):

- ``"payload_too_large"``  — HTTP 413 (request payload rejected by gateway).
- ``"context_overflow"``   — the prompt exceeded the model's context window.
- ``None``                 — not a target error; the caller MUST re-raise.

Classification is a fixed pattern table (not per-provider adapters) because
20+ providers in models/providers/registry.py raise errors of wildly varying
shapes: some carry a ``status_code`` attribute, others only message strings,
and LangChain may wrap the real cause. Hence the dual-channel rules:

- status_code channel: ``getattr(exc, "status_code", None) == 413``.
- string channel: case-insensitive match over the exception text, expanded
  through the ``__cause__`` / ``__context__`` chain up to depth 3.

The classifier is READ-ONLY: it never raises on normal (and most abnormal)
input, never swallows an exception — it only classifies and returns. No
provider SDK is imported at module level (duck-typing only).
"""

from collections.abc import Iterator

from loguru import logger

PAYLOAD_TOO_LARGE = "payload_too_large"
CONTEXT_OVERFLOW = "context_overflow"

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


def classify_provider_error(exc: BaseException) -> str | None:
    """Classify a provider exception for the retry loop.

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

    text = "\n".join(_exception_chain_texts(exc))
    result = _match_payload_too_large(exc, text) or _match_context_overflow(text)
    logger.debug("classify_provider_error: {} -> {}", type(exc).__name__, result)
    return result


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


def _match_payload_too_large(exc: BaseException, text: str) -> str | None:
    """HTTP 413 via a status_code attribute, or a 413 payload/too-large string."""
    if getattr(exc, "status_code", None) == _STATUS_CODE_TOO_LARGE:
        return PAYLOAD_TOO_LARGE
    lowered = text.lower()
    if "413" in lowered and any(hint in lowered for hint in _PAYLOAD_TEXT_HINTS):
        return PAYLOAD_TOO_LARGE
    return None


def _match_context_overflow(text: str) -> str | None:
    """Any fixed context-window-exceeded pattern (case-insensitive)."""
    lowered = text.lower()
    if any(pattern in lowered for pattern in _CONTEXT_OVERFLOW_PATTERNS):
        return CONTEXT_OVERFLOW
    return None
