"""Shared helpers and base classes for agent middlewares (audit 1.1)."""

import hashlib
import json
from typing import Any

from loguru import logger


# ---------------------------------------------------------------------------
# 1.1.10 require_session_id
# ---------------------------------------------------------------------------

def require_session_id(state: dict[str, Any], error_message: str) -> str:
    """Extract and validate session_id from state.

    Args:
        state: The agent state dict.
        error_message: The exact error message to raise if session_id is
            missing or blank. Allows each middleware to preserve its exact
            error wording (e.g. "Not pass session_id" or
            "HeartbeatStaleness: session_id is required").

    Returns:
        The validated session_id string.

    Raises:
        RuntimeError: If session_id is missing or blank, with the
            provided error_message.
    """
    session_id: str = state.get("session_id", "")
    if not session_id.strip():
        raise RuntimeError(error_message)
    return session_id



# ---------------------------------------------------------------------------
# 1.1.9 Sync/Async hook bridge mixins
# ---------------------------------------------------------------------------

class BeforeAgentHooksMixin:
    """Mixin providing sync/async before_agent hooks that delegate to
    ``_before_agent_impl``.

    Subclasses must implement ``_before_agent_impl(self, state) -> None``.
    The sync and async hooks log the hook name and delegate to the impl,
    returning ``None`` to match the langchain AgentMiddleware contract.
    """

    def before_agent(self, state: Any, runtime: Any = None) -> None:
        logger.debug("{} before_agent hook fired", type(self).__name__)
        self._before_agent_impl(state)
        return None

    async def abefore_agent(self, state: Any, runtime: Any = None) -> None:
        logger.debug("{} abefore_agent hook fired", type(self).__name__)
        self._before_agent_impl(state)
        return None


class AfterAgentHooksMixin:
    """Mixin providing sync/async after_agent hooks that delegate to
    ``_after_agent_impl``.

    Subclasses must implement ``_after_agent_impl(self, state) -> None``.
    The sync and async hooks log the hook name and delegate to the impl,
    returning ``None`` to match the langchain AgentMiddleware contract.
    """

    def after_agent(self, state: Any, runtime: Any = None) -> None:
        logger.debug("{} after_agent hook fired", type(self).__name__)
        self._after_agent_impl(state)
        return None

    async def aafter_agent(self, state: Any, runtime: Any = None) -> None:
        logger.debug("{} aafter_agent hook fired", type(self).__name__)
        self._after_agent_impl(state)
        return None


# ---------------------------------------------------------------------------
# 1.1.8 _args_hash
# ---------------------------------------------------------------------------

def args_hash(args: dict[str, Any]) -> str:
    """Return an MD5 hash of serialized tool arguments for deduplication.

    The hash is deterministic: keys are sorted, non-serializable values
    fall back to ``str()`` representation.

    Args:
        args: The tool arguments dict.

    Returns:
        A 32-character hexadecimal MD5 digest.
    """
    try:
        serialized = json.dumps(args, sort_keys=True, default=str)
    except (TypeError, ValueError):
        serialized = str(args)
    return hashlib.md5(serialized.encode()).hexdigest()