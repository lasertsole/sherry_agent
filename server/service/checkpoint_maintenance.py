"""Boot-time checkpoint housekeeping.

The global keep-latest prune belongs here rather than in ``built_agent()``:
that function runs once per turn, so pruning there put a competing writer on
the shared checkpoint DB every turn (``SQLITE_BUSY`` turn failures, plus a
window where another session's brand-new checkpoint could be deleted by a
prune whose keep-set was computed before the commit). Once per process is
enough — the per-thread prune still runs on child release and session purge.
"""

from __future__ import annotations

from loguru import logger

__all__ = ["clean_checkpoints_at_boot"]


async def clean_checkpoints_at_boot() -> dict[str, int] | None:
    """Run the one global prune; ``None`` when it failed (fail-open).

    A maintenance pass must never keep the backend from booting: a locked or
    corrupt DB logs a warning and the server serves.
    """
    try:
        from agent.checkpointer.thread_safe_checkpointer import aclean_all_checkpoints

        result = await aclean_all_checkpoints()
        logger.info(
            "checkpoint maintenance: kept {} thread(s), deleted {} checkpoint(s) / {} write(s)",
            result.get("threads_kept", 0),
            result.get("deleted_checkpoints", 0),
            result.get("deleted_writes", 0),
        )
        return result
    except Exception as e:  # noqa: BLE001 - housekeeping is always best-effort
        logger.warning("checkpoint maintenance skipped: {}", e)
        return None
