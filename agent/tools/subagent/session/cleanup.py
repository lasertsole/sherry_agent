"""Cleanup logic for sub-agent sessions.

Deletes child session state via EventBus, preserving the injected event and
transcript-deletion flags.
"""

from loguru import logger
from ..events import InboundMessage, get_event_bus


async def delete_subagent_session_for_cleanup(
    child_session_key: str,
) -> None:
    """Best-effort cleanup of a child sub-agent session via EventBus.

    Sends a delete signal as an InboundMessage to the child session,
    matching the pattern used by delivery.py and send.py.
    """
    try:
        msg = InboundMessage(
            channel="system",
            sender_id="subagent_cleanup",
            chat_id="direct",
            content="__session_delete__",
            session_id=child_session_key,
            metadata={
                "injected_event": "session_delete",
                "delete_transcript": True,
            },
        )
        bus = get_event_bus()
        await bus.publish_internal(msg)
        logger.debug("Session cleanup completed for: {}", child_session_key)
    except Exception as e:
        logger.warning("Session cleanup failed for {}: {}", child_session_key, e)
