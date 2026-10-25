"""Retry / backoff defaults for the LLM retry middleware (ZCode parity)."""

from typing import Literal, TypedDict


class LlmRetryConfig(TypedDict):
    """Bounded retry / backoff defaults for the LLM retry middleware."""

    #: Maximum RETRIES after the first attempt (total attempts = this + 1).
    max_retries: int
    base_delay: float
    #: Exponential factor between attempts (ZCode uses 2).
    backoff_factor: float
    max_delay: float
    #: "full" = ZCode's 0.5–1.0× jitter; "symmetric" = historical ±jitter.
    jitter_mode: Literal["full", "symmetric", "none"]
    jitter: float
    respect_retry_after: bool
    retry_after_cap_s: float
    #: Extra attempts for a completion with no text, no tool calls and no usage.
    empty_completion_retries: int
    #: Push a `model_retry` WS frame + trajectory event per retry.
    emit_retry_events: bool
    #: Maximum fallback-model switches per user turn (ZCode allows one); 0
    #: disables fallback for the turn, including a sticky candidate.
    max_fallback_switches: int
    #: "turn" resets the timeout streak at every main-agent turn; "session"
    #: keeps the old cross-turn breaker.
    stale_giveup_scope: Literal["turn", "session"]
    stale_giveup_threshold: int


LLM_RETRY: LlmRetryConfig = {
    "max_retries": 10,
    "base_delay": 2.0,
    "backoff_factor": 2.0,
    "max_delay": 60.0,
    "jitter_mode": "full",
    "jitter": 0.3,
    "respect_retry_after": True,
    "retry_after_cap_s": 300.0,
    "empty_completion_retries": 1,
    "emit_retry_events": True,
    "max_fallback_switches": 1,
    "stale_giveup_scope": "turn",
    "stale_giveup_threshold": 5,
}
