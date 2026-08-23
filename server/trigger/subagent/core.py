"""Subagent system startup hook.

The sub-agent library now owns both its own EventBus and the delivery
consumer (``agent.tools.subagent.events.bridge._consume_loop``). The old
``subagent_manager.set_consumer(...)`` / ``start_service()`` pattern was
removed during the future_subagent → agent.tools.subagent migration; result
delivery is pushed directly to sessions by the bridge's single consumer.

This module only schedules once-at-startup registration (
``init_registry()``, which creates DB tables, restores persisted runs, and
starts the EventBus bridge) onto the shared channel event loop.
"""

import asyncio
from loguru import logger
from channels.manager import channel_manager


async def _init_registry_once() -> None:
    """Run registry initialization (idempotent) and start the EventBus bridge."""
    try:
        from agent.tools.subagent.registry import init_registry
        await init_registry()
        logger.info("Subagent registry initialized via server startup hook")
    except Exception as e:  # pragma: no cover - startup robustness
        logger.error("Failed to initialize subagent registry at startup: {}", e)


def _schedule_startup() -> None:
    """Schedule ``init_registry`` onto the running channel event loop.

    Runs once at import time (side-effect registration), matching the
    heartbeat / channel startup pattern in channels/core.py.
    """
    try:
        event_loop: asyncio.AbstractEventLoop | None = channel_manager.get_event_loop()
    except Exception:
        event_loop = None

    if event_loop is not None and event_loop.is_running():
        _ = event_loop.create_task(_init_registry_once())
    elif event_loop is not None:
        # No live loop yet — register a best-effort callback so the task
        # runs once the channel manager starts its loop.
        _ = event_loop.run_in_executor(None, asyncio.run, _init_registry_once())
        logger.info("Subagent init_registry scheduled when channel event loop becomes available")


_schedule_startup()

