"""Cleanup logic for sub-agent sessions.

Deletes child session state via gateway, preserving lifecycle hooks for
session-mode spawns.
"""

from loguru import logger
from ..types.spawn import SpawnMode


async def delete_subagent_session_for_cleanup(
    child_session_key: str,
    spawn_mode: SpawnMode = SpawnMode.RUN,
) -> None:
    """Best-effort cleanup of a child sub-agent session via gateway delete.

    Emits lifecycle hooks only when spawn_mode is SESSION.
    """
    try:
        from bus import publish_inbound

        await publish_inbound(
            target_session_key=child_session_key,
            method="sessions.delete",
            params={
                "key": child_session_key,
                "delete_transcript": True,
                "emit_lifecycle_hooks": spawn_mode == SpawnMode.SESSION,
            },
        )
        logger.debug("Session cleanup completed for: {}", child_session_key)
    except Exception as e:
        logger.warning("Session cleanup failed for {}: {}", child_session_key, e)
