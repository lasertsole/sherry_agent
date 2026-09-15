"""Lane runtime lifecycle: startup wiring and a bounded shutdown drain.

``install_lane_lifecycle()`` runs once from ``server/__main__`` before
``app.start()``:

1. ``validate_lane_config()`` — a bad lane config fails fast at startup.
2. ``get_lane_manager()`` — eagerly builds the process-level singleton
   (lane semaphores stay lazily bound to the loop that first uses them).
3. ``set_drain_check(is_gateway_draining)`` — new lane acquires are refused
   while the subagent gateway reports draining (callback injection keeps
   ``runtime/lane`` free of upward imports).
4. a bounded best-effort drain on the existing sync ``atexit`` seam.

Shutdown reality check: the repo has no async shutdown path.
``stop_sweeper()`` is the subagent lifecycle stop gate but has no production
caller, Robyn 0.84's ``shutdown_handler`` is registered yet never invoked on
SIGINT/SIGTERM (verified with a live probe), and ``atexit`` is synchronous.
The exit drain is therefore bounded to ``timeout=0`` — it flips drain mode,
reports the final per-lane counters, and never delays process exit.
In-flight turns are abandoned to the OS (plan §14: acceptable).
"""

from __future__ import annotations

import asyncio
import atexit

from loguru import logger

_installed = False


def install_lane_lifecycle() -> None:
    """Validate the lane config, prewarm the manager, and wire the drain gate."""
    global _installed
    if _installed:
        return

    from agent.tools.subagent.registry import is_gateway_draining
    from config.features.infra_side.lane_system import validate_lane_config
    from runtime.lane.core import get_lane_manager, set_drain_check

    validate_lane_config()
    get_lane_manager()
    set_drain_check(is_gateway_draining)
    atexit.register(_drain_lanes_at_exit)
    _installed = True
    logger.info("Lane lifecycle installed (config validated, drain gate registered)")


async def drain_all_lanes(timeout: float | None = None) -> dict[str, bool]:
    """Awaitable drain seam for a real async shutdown path."""
    from runtime.lane.core import get_lane_manager

    return await get_lane_manager().drain_all(timeout)


def _drain_lanes_at_exit() -> None:
    """atexit seam: stop admitting lane work, then report the final state."""
    from agent.tools.subagent.registry import set_draining

    set_draining(True)
    try:
        results = asyncio.run(drain_all_lanes(timeout=0))
        logger.info("Lane drain at exit (bounded, timeout=0): {}", results)
    except Exception:
        logger.exception("Lane drain at exit failed (fail-open)")
