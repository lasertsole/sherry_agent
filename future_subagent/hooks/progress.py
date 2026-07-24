"""Progress hooks for sub-agent lifecycle: spawned, progress, ended, and delivery-target."""

from loguru import logger
from ..types.registry import SubagentRunRecord

_spawned_hooks: list = []
_progress_hooks: list = []
_ended_hooks: list = []
_delivery_target_hooks: list = []


def register_spawned_hook(hook) -> None:
    """Register a hook called when a sub-agent is spawned."""
    _spawned_hooks.append(hook)


def register_progress_hook(hook) -> None:
    """Register a hook called during sub-agent execution progress."""
    _progress_hooks.append(hook)


def register_ended_hook(hook) -> None:
    """Register a hook called when a sub-agent ends."""
    _ended_hooks.append(hook)


def register_delivery_target_hook(hook) -> None:
    """Register a hook that can redirect the delivery target session key."""
    _delivery_target_hooks.append(hook)


def clear_all_hooks() -> None:
    """Remove all registered progress hooks."""
    _spawned_hooks.clear()
    _progress_hooks.clear()
    _ended_hooks.clear()
    _delivery_target_hooks.clear()


async def fire_spawned_hook(run: SubagentRunRecord) -> None:
    """Fire all spawned hooks for a run; errors are logged and swallowed."""
    for hook in _spawned_hooks:
        try:
            await hook(run)
        except Exception as e:
            logger.debug("Spawned hook error for run {}: {}", run.run_id, e)


async def fire_progress_hook(run: SubagentRunRecord, message: str = "") -> None:
    """Fire all progress hooks for a run; errors are logged and swallowed."""
    for hook in _progress_hooks:
        try:
            await hook(run, message)
        except Exception as e:
            logger.debug("Progress hook error for run {}: {}", run.run_id, e)


async def fire_ended_hook(run: SubagentRunRecord) -> None:
    """Fire all ended hooks for a run; errors are logged and swallowed."""
    for hook in _ended_hooks:
        try:
            await hook(run)
        except Exception as e:
            logger.debug("Ended hook error for run {}: {}", run.run_id, e)


async def fire_delivery_target_hook(run: SubagentRunRecord, target_session_key: str) -> str | None:
    """Fire delivery-target hooks; the first hook returning a non-None redirect wins."""
    for hook in _delivery_target_hooks:
        try:
            redirect = await hook(run, target_session_key)
            if redirect is not None:
                return redirect
        except Exception as e:
            logger.debug("Delivery target hook error for run {}: {}", run.run_id, e)
    return None
