"""Retry delay policy: exponential backoff, jitter and ``Retry-After``.

Ported from ZCode's model adapter retry policy and kept dependency-free so the
LangChain middleware, provider clients and tests can share one implementation.

- default curve: ``base_delay * factor**(attempt-1)`` capped at ``max_delay``
- ``jitter_mode="full"`` → ``capped * (0.5 + random*0.5)`` (ZCode parity)
- ``jitter_mode="symmetric"`` → ±``jitter`` fraction (the historical Sherry form)
- a reasonable provider ``Retry-After`` wins over the local curve (no jitter)
"""

from __future__ import annotations

import random
from collections.abc import Mapping
from datetime import datetime, UTC
from email.utils import parsedate_to_datetime
from typing import Any

__all__ = [
    "compute_retry_delay",
    "extract_retry_after_seconds",
    "parse_retry_after_seconds",
]

#: A provider may send an absurd Retry-After; never sleep past this by default.
DEFAULT_RETRY_AFTER_CAP_S = 300.0


def _header(headers: Mapping[str, Any] | None, name: str) -> Any:
    if not headers:
        return None
    wanted = name.lower()
    try:
        for key, value in headers.items():
            if str(key).lower() == wanted:
                return value
    except Exception:  # noqa: BLE001 - a broken header mapping is "nothing"
        return None
    return None


def parse_retry_after_seconds(headers: Mapping[str, Any] | None) -> float | None:
    """Parse ``retry-after-ms`` / ``retry-after`` from a response-header mapping.

    Supports milliseconds, integer/float seconds and an HTTP-date. Invalid or
    non-positive values are ignored.
    """
    raw_ms = _header(headers, "retry-after-ms")
    if raw_ms is not None:
        try:
            milliseconds = float(str(raw_ms).strip())
            if milliseconds > 0:
                return milliseconds / 1000.0
        except (TypeError, ValueError):
            pass

    raw = _header(headers, "retry-after")
    if raw is None:
        return None
    text = str(raw).strip()
    if not text:
        return None
    try:
        seconds = float(text)
        return seconds if seconds > 0 else None
    except ValueError:
        pass
    try:
        when = parsedate_to_datetime(text)
    except (TypeError, ValueError, OverflowError):
        return None
    if when is None:
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    seconds = (when - datetime.now(UTC)).total_seconds()
    return seconds if seconds > 0 else None


def _response_headers(exc: BaseException) -> Mapping[str, Any] | None:
    """Best-effort headers off an SDK exception, following its cause chain."""
    current: BaseException | None = exc
    seen: set[int] = set()
    for _ in range(4):
        if current is None or id(current) in seen:
            return None
        seen.add(id(current))
        for candidate in (getattr(current, "headers", None),):
            if isinstance(candidate, Mapping):
                return candidate
        response = getattr(current, "response", None)
        headers = getattr(response, "headers", None)
        if isinstance(headers, Mapping):
            return headers
        current = current.__cause__ or current.__context__
    return None


def extract_retry_after_seconds(exc: BaseException) -> float | None:
    """Provider ``Retry-After`` carried by an SDK exception, in seconds."""
    return parse_retry_after_seconds(_response_headers(exc))


def compute_retry_delay(
    attempt: int,
    *,
    base_delay: float,
    backoff_factor: float = 2.0,
    max_delay: float = 60.0,
    jitter_mode: str = "full",
    jitter: float = 0.3,
    retry_after_s: float | None = None,
    retry_after_cap_s: float = DEFAULT_RETRY_AFTER_CAP_S,
    respect_retry_after: bool = True,
) -> float:
    """Seconds to sleep before retry ``attempt`` (1 = first retry).

    ``retry_after_s`` is used when it is positive and not above the cap; it is
    not jittered (the provider told us exactly how long to wait).
    """
    delay = max(0.0, float(base_delay)) * max(1.0, float(backoff_factor)) ** max(0, attempt - 1)
    capped = min(delay, max(0.0, float(max_delay)))
    if respect_retry_after and retry_after_s is not None:
        try:
            hinted = float(retry_after_s)
        except (TypeError, ValueError):
            hinted = -1.0
        if 0 < hinted <= max(0.0, float(retry_after_cap_s)):
            return hinted
    if capped <= 0:
        return 0.0
    if jitter_mode == "none":
        return round(capped, 3)
    if jitter_mode == "symmetric":
        amount = capped * max(0.0, float(jitter)) * (random.random() * 2 - 1)
        return max(0.0, min(float(max_delay), capped + amount))
    # Full jitter: keep the provider's jittered wait inside [0.5×, 1×] capped.
    return round(capped * (0.5 + random.random() * 0.5), 3)
