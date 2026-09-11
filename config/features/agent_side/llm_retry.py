"""Bounded retry / jittered-backoff defaults for the LLM retry middleware."""

from typing import TypedDict


class LlmRetryConfig(TypedDict):
    """Bounded retry / jittered-backoff defaults for the LLM retry middleware."""

    max_retries: int
    base_delay: float
    max_delay: float
    jitter: float
    stale_giveup_threshold: int


LLM_RETRY: LlmRetryConfig = {
    "max_retries": 3,
    "base_delay": 2.0,
    "max_delay": 60.0,
    "jitter": 0.3,
    "stale_giveup_threshold": 5,
}
