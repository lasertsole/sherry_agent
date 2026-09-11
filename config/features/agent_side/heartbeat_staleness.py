"""Heartbeat watchdog cadence and stale-cycle kill thresholds."""

from typing import TypedDict


class HeartbeatStalenessConfig(TypedDict):
    """Heartbeat watchdog cadence and stale-cycle kill thresholds."""

    heartbeat_interval_minutes: int
    stale_cycles_idle: int
    stale_cycles_in_tool: int


HEARTBEAT_STALENESS: HeartbeatStalenessConfig = {
    "heartbeat_interval_minutes": 1,
    "stale_cycles_idle": 7,
    "stale_cycles_in_tool": 20,
}
