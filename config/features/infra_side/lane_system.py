"""Global lane concurrency limits for agent work dispatch."""

import os
from typing import TypedDict


class LaneSystemConfig(TypedDict):
    """Per-lane concurrency limits. Values are slot counts (>= 1)."""

    main_max_concurrent: int
    subagent_max_concurrent: int
    nudge_max_concurrent: int
    nested_max_concurrent: int
    lane_wait_warn_ms: int
    lane_drain_timeout_seconds: float


DEFAULT_SUBAGENT_MAX_CONCURRENT = 8
DEFAULT_NUDGE_MAX_CONCURRENT = 4

_MAIN_SCALED_MIN = 8
_MAIN_SCALED_MAX = 16


def _resolve_main_concurrency() -> int:
    """CPU-scaled main-lane limit, bounded to 12~16 and always invariant-safe.

    Mirrors OpenClaw's ``resolveAgentMaxConcurrent`` for the CPU-scaling part
    (``min(16, max(8, CPU))``), then clamps up to the hard invariant
    ``main >= SUBAGENT + NUDGE`` (8 + 4 = 12 by default). Without that clamp
    the factory default would be 8 on any machine with <= 8 CPUs and
    ``validate_lane_config()`` would raise at startup, so the clamp keeps the
    shipped default valid on 4/8/12/16/64-core machines alike.
    """
    cpu = os.cpu_count() or _MAIN_SCALED_MIN
    cpu_scaled = min(_MAIN_SCALED_MAX, max(_MAIN_SCALED_MIN, cpu))
    invariant_floor = DEFAULT_SUBAGENT_MAX_CONCURRENT + DEFAULT_NUDGE_MAX_CONCURRENT
    return min(_MAIN_SCALED_MAX, max(cpu_scaled, invariant_floor))


LANE_SYSTEM: LaneSystemConfig = {
    "main_max_concurrent": _resolve_main_concurrency(),
    "subagent_max_concurrent": DEFAULT_SUBAGENT_MAX_CONCURRENT,
    "nudge_max_concurrent": DEFAULT_NUDGE_MAX_CONCURRENT,
    "nested_max_concurrent": 1,
    "lane_wait_warn_ms": 5000,
    "lane_drain_timeout_seconds": 30.0,
}


def validate_lane_config() -> None:
    """Validate lane config constraints.

    Called at server startup (never at import time) so a bad override fails
    fast with a clear message instead of deadlocking at runtime.
    """
    cfg = LANE_SYSTEM
    if cfg["main_max_concurrent"] < cfg["subagent_max_concurrent"] + cfg["nudge_max_concurrent"]:
        raise ValueError(
            f"MAIN lane ({cfg['main_max_concurrent']}) must be >= "
            f"SUBAGENT ({cfg['subagent_max_concurrent']}) + NUDGE ({cfg['nudge_max_concurrent']})"
        )
    if cfg["subagent_max_concurrent"] < 1:
        raise ValueError("subagent_max_concurrent must be >= 1")
    if cfg["nudge_max_concurrent"] < 1:
        raise ValueError("nudge_max_concurrent must be >= 1")
    if cfg["nested_max_concurrent"] < 1:
        raise ValueError("nested_max_concurrent must be >= 1")
