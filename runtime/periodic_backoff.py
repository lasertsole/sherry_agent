"""PeriodicBackoff — exponential backoff state for periodic services.

Tracks consecutive failures of a periodic task (cron / heartbeat / sweeper)
and grows the retry interval exponentially: ``base_interval * factor^n``,
capped at ``max_interval``. After ``max_consecutive_failures`` consecutive
failures the backoff is exhausted (the service should enter a degraded /
disabled state). A success fully resets the component.

Pure state machine: no threads, no locks, no IO — each consumer owns its
own instance and uses it single-threaded.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class PeriodicBackoff:
    """Exponential backoff for a periodic service's failure/retry cycle."""

    base_interval: float
    factor: float = 2.0
    max_interval: float = 7200.0
    max_consecutive_failures: int = 5
    current_interval: float = field(init=False)
    consecutive_failures: int = 0
    reason: str = ""
    exhausted: bool = False

    def __post_init__(self) -> None:
        self.current_interval = self.base_interval

    def record_failure(self, reason: str) -> None:
        """Record one failure: bump the counter, grow the interval, set exhaustion."""
        self.consecutive_failures += 1
        self.current_interval = min(
            self.base_interval * self.factor**self.consecutive_failures,
            self.max_interval,
        )
        self.reason = reason
        if self.consecutive_failures >= self.max_consecutive_failures:
            self.exhausted = True

    def record_success(self) -> None:
        """Full reset: interval, failure count, exhaustion and reason."""
        self.consecutive_failures = 0
        self.current_interval = self.base_interval
        self.exhausted = False
        self.reason = ""

    def is_exhausted(self) -> bool:
        """Whether consecutive failures reached ``max_consecutive_failures``."""
        return self.exhausted
