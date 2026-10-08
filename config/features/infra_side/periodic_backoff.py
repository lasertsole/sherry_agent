"""Periodic-task backoff defaults."""

from typing import TypedDict


class PeriodicBackoffConfig(TypedDict):
    """Periodic-task backoff defaults."""

    factor: float
    max_interval_s: float
    max_consecutive_failures: int


PERIODIC_BACKOFF: PeriodicBackoffConfig = {
    "factor": 2.0,
    "max_interval_s": 7200.0,
    "max_consecutive_failures": 5,
}
