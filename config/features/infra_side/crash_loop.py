"""Crash-loop breaker window, trip threshold and retention."""

from typing import TypedDict


class CrashLoopConfig(TypedDict):
    """Crash-loop breaker window, trip threshold and retention."""

    window_s: int
    trip_threshold: int
    retention_s: int
    reason_max_len: int


CRASH_LOOP: CrashLoopConfig = {
    "window_s": 300,
    "trip_threshold": 3,
    "retention_s": 3600,
    "reason_max_len": 200,
}
