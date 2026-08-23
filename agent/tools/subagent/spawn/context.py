"""Prepare the initial message list for a sub-agent based on its context inheritance mode."""

from ..types.spawn import ContextMode
from loguru import logger


async def prepare_spawned_context(
    context_mode: ContextMode,
    requester_session_id: str | None = None,
) -> list:
    """Decide whether the child inherits parent messages based on context mode.

    ISOLATED: no context inheritance.
    FORK: copy the full message history from the parent's checkpointer.
    """
    if context_mode == ContextMode.ISOLATED:
        return []

    if context_mode == ContextMode.FORK:
        if not requester_session_id:
            logger.warning("FORK context requested but no requester_session_id provided, falling back to isolated")
            return []

        try:
            from agent import built_agent
            from pub_func import build_agent_config

            agent = await built_agent()
            state = await agent.aget_state(config=build_agent_config(requester_session_id))
            messages = state.values.get("messages", [])

            if messages:
                logger.info("FORK context: copied {} messages from parent session {}", len(messages), requester_session_id)
                return list(messages)

            logger.info("FORK context: no messages found in parent session {}", requester_session_id)
            return []
        except Exception as e:
            logger.error("FORK context: failed to read parent history for session {}: {}", requester_session_id, e)
            return []

    return []
