"""Jittered and adaptive retry-backoff defaults."""

from typing import TypedDict


class RetryBackoffConfig(TypedDict):
    """Jittered and adaptive retry-backoff defaults."""

    jittered_base_delay: float
    jittered_max_delay: float
    jittered_jitter: float
    backoff_floor: float
    adaptive_base_delay: float
    adaptive_max_delay: float


RETRY_BACKOFF: RetryBackoffConfig = {
    "jittered_base_delay": 2.0,
    "jittered_max_delay": 60.0,
    "jittered_jitter": 0.3,
    "backoff_floor": 0.1,
    "adaptive_base_delay": 5.0,
    "adaptive_max_delay": 120.0,
}
