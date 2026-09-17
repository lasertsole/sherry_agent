"""System-prompt injection for every model call, built with ``@dynamic_prompt``.

``system_prompt_injection`` is the middleware instance produced by LangChain's
``@dynamic_prompt`` decorator. It owns the **outermost** ``wrap_model_call``
layer of the main agent (first in the middleware list that implements the
hook), so the session system prompt is resolved before any other model-call
wrapper runs.

Two properties are load-bearing:

1. **Three-tier prompt cache** — ``state_register_mem`` → ``state_register_db``
   → ``workspace.prompt_builder.build_system_prompt``. A full miss writes both
   registers. The ``system_prompt`` mem key is a contract with the
   compression pipeline: ``Summarization`` reads it for token estimation and
   rewrites it after a compression.
2. **Same-content skip** — the decorator's generated wrapper *unconditionally*
   calls ``request.override(system_message=...)``. Returning the request's
   existing ``SystemMessage`` object when the content is unchanged keeps the
   serialized model-visible prefix byte-identical (provider prefix cache),
   while a changed prompt — or a first call, where ``request.system_message``
   is ``None`` — returns a fresh ``SystemMessage``. The previous
   ``AgentMiddleware`` subclass could return the request itself; the decorator
   cannot skip the override, so object identity of the *message* (not the
   request) carries the byte-identity guarantee. This is the intentional
   semantic change of the migration.
"""

from loguru import logger
from langgraph.typing import ContextT
from langchain.agents.middleware import ModelRequest, dynamic_prompt
from langchain_core.messages import SystemMessage
from workspace.prompt_builder import build_system_prompt
from runtime import state_register_db, state_register_mem
from agent.middlewares.base import require_session_id

__all__ = ["system_prompt_injection"]


def _get_and_reload_system_prompt(session_id: str) -> str:
    """Resolve the session system prompt through the three-tier cache.

    Tier 1: a ``state_register_mem`` hit returns immediately. Tier 2: a
    ``state_register_db`` hit is promoted back into mem. A full miss rebuilds
    via ``build_system_prompt`` and dual-writes db + mem.
    """
    system_prompt = state_register_mem.get_state(session_id, "system_prompt", None)

    if system_prompt is None:
        system_prompt = state_register_db.get_state(session_id, "system_prompt", None)

        if system_prompt is None:
            system_prompt = build_system_prompt(session_id=session_id)
            state_register_db.set_state(session_id, "system_prompt", system_prompt)

        state_register_mem.set_state(session_id, "system_prompt", system_prompt)

    return system_prompt


@dynamic_prompt
def system_prompt_injection(request: ModelRequest[ContextT]) -> SystemMessage:
    """Inject the session system prompt, reusing an identical existing message.

    Returns ``request.system_message`` itself (same object) when it already
    carries the resolved prompt, so the decorator's unconditional override
    re-applies identical bytes. Otherwise returns a fresh ``SystemMessage``.
    """
    session_id = require_session_id(request.state, "Not pass session_id")
    prompt_str = _get_and_reload_system_prompt(session_id)

    existing = request.system_message
    if isinstance(existing, SystemMessage) and existing.content == prompt_str:
        logger.debug("system_prompt_injection reuses cached system prompt for {}", session_id)
        return existing

    logger.debug("system_prompt_injection injects system prompt for {}", session_id)
    return SystemMessage(content=prompt_str)
