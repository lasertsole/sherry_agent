"""Boot wiring for the restart-recovery effect queue.

Restart recovery (terminal declarations + intent replay) runs in the normal
synchronous boot via ``asyncio.run``; the worker itself needs the server's
long-lived loop, so it is started from a Robyn startup handler (see
``server/__main__.py``). Both entries are no-ops unless the ``effects.*`` flags
enable them.
"""

from __future__ import annotations

from loguru import logger

__all__ = ["run_restart_recovery", "start_worker"]


async def run_restart_recovery() -> None:
    """Apply declared restart terminals and replay in-flight intents."""
    from runtime.effects.config import effects_enabled, restart_replay

    if not (effects_enabled() and restart_replay()):
        return
    from runtime.effects import intent_log
    from server.service.effects.restart_table import apply_all_terminals

    applied = await apply_all_terminals()
    if applied:
        logger.info("effects: restart terminals applied: {}", applied)
    pending = intent_log.replay_pending()
    logger.info("effects: {} intent(s) pending after replay", len(pending))


async def start_worker() -> None:
    """Register the built-in handlers and start the worker (server loop only)."""
    from runtime.effects.config import effects_enabled, shadow_mode, worker_enabled

    if not (effects_enabled() and worker_enabled() and not shadow_mode()):
        return
    from server.service.effects.handlers import (
        handle_auto_turn,
        handle_completion_notice,
        handle_continuation,
    )
    from runtime.effects.worker import get_worker

    worker = get_worker()
    worker.register("continuation", handle_continuation)
    worker.register("completion_notice", handle_completion_notice)
    worker.register("auto_turn", handle_auto_turn)
    worker.start()
