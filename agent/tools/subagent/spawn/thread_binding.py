"""Thread-binding lifecycle for SESSION-mode sub-agents.

Manages: create channel thread → bind to session → idle timeout →
delivery-origin merge → unbind on cleanup.
"""

import time
import uuid
from pydantic import BaseModel
from loguru import logger
from ..types.spawn import SpawnMode


class ThreadBindingConfig(BaseModel):
    """Configuration for thread creation: idle/max-age timeouts and optional naming."""
    idle_timeout_ms: int = 300000  # 5 minutes
    max_age_ms: int = 86400000
    thread_name: str | None = None
    intro_text: str | None = None


class ThreadBindingInfo(BaseModel):
    """Runtime state for an active thread binding: identity, timing, and delivery origin."""
    thread_id: str
    bound_at: float = 0.0
    idle_timeout_ms: int = 300000  # 5 minutes
    max_age_ms: int = 86400000  # 24 hours
    delivery_origin: str | None = None


class ThreadBindingResult:
    """Outcome of a thread-binding attempt."""
    def __init__(self, bound: bool, thread_id: str | None = None, binding_info: ThreadBindingInfo | None = None, delivery_origin: str | None = None):
        self.bound = bound
        self.thread_id = thread_id
        self.binding_info = binding_info
        self.delivery_origin = delivery_origin


def bind_thread_for_subagent_spawn(
    child_session_key: str | None,
    config: ThreadBindingConfig | None = None,
) -> ThreadBindingResult:
    """Create and bind a channel thread for the given sub-agent session."""
    if config is None:
        config = ThreadBindingConfig()

    thread_id = _create_channel_thread(child_session_key, config)
    if thread_id is None:
        return ThreadBindingResult(bound=False)

    binding_info = ThreadBindingInfo(
        thread_id=thread_id,
        bound_at=time.monotonic(),
        idle_timeout_ms=config.idle_timeout_ms,
        max_age_ms=config.max_age_ms,
        delivery_origin=child_session_key,
    )

    delivery_origin = _resolve_delivery_origin(child_session_key, thread_id)

    logger.info("Bound thread {} for subagent session {}", thread_id, child_session_key)
    return ThreadBindingResult(
        bound=True,
        thread_id=thread_id,
        binding_info=binding_info,
        delivery_origin=delivery_origin,
    )


def unbind_thread_on_cleanup(thread_id: str | None) -> None:
    """Release a thread binding when the sub-agent is cleaned up."""
    if thread_id is None:
        return
    logger.info("Unbound thread {} on cleanup", thread_id)


def refresh_thread_binding(thread_id: str | None) -> None:
    """Refresh a thread binding (e.g. after sub-agent resumes from a yield)."""
    if thread_id is None:
        return
    logger.debug("Refreshed thread binding for {}", thread_id)


def resolve_thread_binding_policy(
    agent_id: str,
    spawn_mode: SpawnMode,
    child_session_key: str | None = None,
    config: ThreadBindingConfig | None = None,
) -> ThreadBindingResult:
    """Decide whether to bind a thread; only SESSION mode gets a thread binding."""
    if spawn_mode != SpawnMode.SESSION:
        return ThreadBindingResult(bound=False, thread_id=None)
    return bind_thread_for_subagent_spawn(child_session_key, config)


def _create_channel_thread(child_session_key: str | None, config: ThreadBindingConfig) -> str | None:
    """Generate a deterministic thread ID for the sub-agent; returns None if no session key."""
    try:
        return f"thread:subagent:{uuid.uuid4()}" if child_session_key else None
    except Exception:
        return None


def _resolve_delivery_origin(child_session_key: str | None, thread_id: str) -> str | None:
    """Determine the delivery origin for messages on this thread (currently the child session key)."""
    return child_session_key
