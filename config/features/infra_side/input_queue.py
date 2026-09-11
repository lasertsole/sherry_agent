"""User-input queue tuning (busy timeout, capacity, expiry, sweeping)."""

from typing import TypedDict


class InputQueueConfig(TypedDict):
    """User-input queue tuning (busy timeout, capacity, expiry, sweeping)."""

    busy_timeout_ms: int
    init_wait_timeout_s: float
    max_active_per_session: int
    expiry_seconds: float
    lock_sweep_threshold: int


INPUT_QUEUE: InputQueueConfig = {
    "busy_timeout_ms": 5000,
    "init_wait_timeout_s": 10.0,
    "max_active_per_session": 20,
    "expiry_seconds": 86400.0,
    "lock_sweep_threshold": 256,
}
