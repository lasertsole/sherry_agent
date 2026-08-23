"""Gateway-independent root work admission: marks root-level work, detects drain mode, and retries on drain completion."""

import asyncio
from loguru import logger

_root_work_tasks: set[asyncio.Task] = set()
_draining: bool = False


def is_gateway_draining() -> bool:
    """Return True if the gateway is in drain mode (new root work is deferred)."""
    return _draining


def set_draining(draining: bool) -> None:
    """Enable or disable gateway drain mode."""
    global _draining
    _draining = draining


async def run_with_work_admission(coro, label: str = "unknown") -> None:
    """Submit a coroutine as root work; defers it if the gateway is draining."""
    if is_gateway_draining():
        logger.info("Gateway draining, scheduling retry for: {}", label)
        asyncio.create_task(_schedule_drain_retry(coro, label, delay=5.0))
        return

    task = asyncio.create_task(_run_and_cleanup(coro, label))
    _root_work_tasks.add(task)
    task.add_done_callback(_root_work_tasks.discard)


async def _run_and_cleanup(coro, label: str) -> None:
    """Execute the coroutine and log any unhandled exception."""
    try:
        await coro
    except Exception as e:
        logger.error("Root work '{}' failed: {}", label, e)


async def _schedule_drain_retry(coro, label: str, delay: float = 5.0) -> None:
    """Wait for drain to end, then re-submit the coroutine; gives up if still draining."""
    await asyncio.sleep(delay)
    if not is_gateway_draining():
        await run_with_work_admission(coro, label)
    else:
        logger.warning("Still draining, giving up on: {}", label)


def pending_root_work_count() -> int:
    """Return the number of root work tasks that have not yet completed."""
    return sum(1 for t in _root_work_tasks if not t.done())
