"""Cron service thresholds and run-history limits."""

from typing import TypedDict


class CronServiceConfig(TypedDict):
    """Cron service thresholds and run-history limits."""

    min_every_ms: int
    max_run_history: int
    degraded_threshold: int
    disabled_threshold: int
    degrade_backoff_base_ms: int
    degrade_backoff_max_ms: int
    ws_session_id: str


CRON_SERVICE: CronServiceConfig = {
    "min_every_ms": 1000,
    "max_run_history": 20,
    "degraded_threshold": 5,
    "disabled_threshold": 10,
    "degrade_backoff_base_ms": 5000,
    "degrade_backoff_max_ms": 300000,
    "ws_session_id": "default",
}
