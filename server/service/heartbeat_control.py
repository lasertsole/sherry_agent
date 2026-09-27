"""Runtime on/off switch for the global heartbeat scheduler.

The heartbeat service is a process-wide singleton started on the channel
manager's event loop at boot. This module is the control surface the UI's
心跳 panel uses: it reports the live state and flips the scheduler on or off
immediately, while persisting the choice to ``sherry.jsonc``
(``heartbeat.enabled``) so the next boot honours it.

Keeping the two in one place matters: a switch that only wrote the config would
look applied while the running loop kept firing, and a switch that only touched
the loop would silently turn itself back on after a restart.
"""

from __future__ import annotations

import asyncio
from typing import Any
from collections.abc import Callable, Coroutine

from loguru import logger

from channels import channel_manager
from config.sherry_settings import get_sherry_setting
from server.service.sherry_config import write_sherry_config
from skills.builtin.core.heartbeat import heartbeat_service

#: sherry.jsonc key holding the persisted choice.
HEARTBEAT_ENABLED_KEY = "heartbeat.enabled"


def _schedule(coro: Coroutine[Any, Any, Any]) -> None:
    """Hand *coro* to the channel event loop (the loop the service lives on).

    A seam rather than an inline call: the loop only exists once the channel
    service is up, and tests replace this to assert the start/stop dispatch
    without an event loop. An unavailable/closed loop must not break the HTTP
    call — the persisted value still applies on the next boot.
    """
    try:
        loop = channel_manager.get_event_loop()
        asyncio.run_coroutine_threadsafe(coro, loop)
    except Exception as exc:
        # An unreachable loop must not make the switch unanswerable: the value is
        # already persisted, so the next boot picks it up.
        coro.close()
        logger.warning("Heartbeat toggle: could not reach the service loop ({})", exc)


async def _stop_on_loop() -> None:
    """Stop callable that runs on the service's own loop (Task.cancel is not
    thread-safe, so the stop must be dispatched there)."""
    heartbeat_service.stop()


def get_heartbeat_status() -> dict[str, Any]:
    """Current heartbeat state.

    ``enabled`` is the choice (what the switch shows), ``running`` the live
    scheduler state — they differ for the moment between a toggle and the loop
    picking the work up, and for a service the boot path left disabled.
    """
    return {
        "enabled": bool(heartbeat_service.enabled),
        "running": bool(heartbeat_service.is_running),
        "interval_s": int(heartbeat_service.interval_s),
    }


def set_heartbeat_enabled(
    enabled: bool, *, persist: Callable[[dict[str, str]], None] | None = None
) -> dict[str, Any]:
    """Apply the global heartbeat switch and return the resulting status.

    Persists the choice first (so a failing write is reported to the caller and
    nothing runs half-applied), then starts or stops the live scheduler.
    """
    value = bool(enabled)
    writer = persist or write_sherry_config
    writer({HEARTBEAT_ENABLED_KEY: "true" if value else "false"})

    heartbeat_service.enabled = value
    if value:
        _schedule(heartbeat_service.start())
    else:
        _schedule(_stop_on_loop())
    logger.info("Heartbeat {} via the panel switch", "enabled" if value else "disabled")
    return get_heartbeat_status()


def apply_persisted_heartbeat_setting() -> bool:
    """Adopt the persisted switch on the boot path (called before ``start()``).

    Returns the value applied; a missing/unparseable setting falls back to the
    typed default (``True``), so the service keeps its historical behaviour.
    """
    value = bool(get_sherry_setting(HEARTBEAT_ENABLED_KEY))
    heartbeat_service.enabled = value
    return value
