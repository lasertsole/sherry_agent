"""Retry backoff utilities — exponential backoff with jitter.

Reference: hermes-agent ``retry_utils.py``. Jitter prevents thundering-herd
retries when many sessions fail against the same provider at once.
"""

import random


def jittered_backoff(
    attempt: int, *, base_delay: float = 2.0, max_delay: float = 60.0, jitter: float = 0.3
) -> float:
    """Exponential backoff for ``attempt`` (1-based) with symmetric jitter.

    The exponential delay ``base_delay * 2**(attempt - 1)`` is capped at
    ``max_delay``, then jittered by up to ±``jitter`` (fraction) of the capped
    delay. The result is clamped to ``[0.1, max_delay]`` so a retry never
    sleeps past the configured ceiling.
    """
    exponential = base_delay * (2 ** (attempt - 1))
    capped = min(exponential, max_delay)
    jitter_amount = capped * jitter * (random.random() * 2 - 1)
    return max(0.1, min(max_delay, capped + jitter_amount))


def adaptive_rate_limit_backoff(
    attempt: int,
    *,
    retry_after: float | None = None,
    base_delay: float = 5.0,
    max_delay: float = 120.0,
) -> float:
    """Backoff for rate-limit (429) responses.

    Honors the provider's ``Retry-After`` hint (capped at ``max_delay``) when
    present and positive; otherwise falls back to :func:`jittered_backoff`.
    """
    if retry_after is not None and retry_after > 0:
        return min(retry_after, max_delay)
    return jittered_backoff(attempt, base_delay=base_delay, max_delay=max_delay)
