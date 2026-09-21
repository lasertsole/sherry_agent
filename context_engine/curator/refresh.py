"""Curator provider resolution and system-prompt refresh.

The orchestrator re-imports these names so the public API and the test-pinned
private surface (``orchestrator._provider_misses_logged``,
``orchestrator._schedule_system_prompt_refresh``) stay on the orchestrator
namespace.
"""

import asyncio
from typing import TYPE_CHECKING

from loguru import logger

if TYPE_CHECKING:
    from runtime.data_provider import PromptDataProvider, SkillWriteProvider


# First miss per provider method is logged once (mirrors runtime.hooks consumers).
_provider_misses_logged: set[str] = set()


def _resolve_provider(method: str) -> "PromptDataProvider | None":
    """Resolve the prompt data provider, logging the first miss per method.

    The provider is registered by ``agent.core.init()`` at server boot; when it
    is missing the refresh is skipped (never caching empty prompts) instead of
    importing the agent package.
    """
    from runtime import data_provider

    provider = data_provider.get_prompt_data_provider()
    if provider is None and method not in _provider_misses_logged:
        _provider_misses_logged.add(method)
        logger.debug(
            "curator: prompt data provider is not registered; '{}' skips the refresh",
            method,
        )
    return provider


def _resolve_skill_writer(method: str) -> "SkillWriteProvider | None":
    """Resolve the skill write provider, logging the first miss per method.

    The provider is registered by ``agent.core.init()`` at server boot; when it
    is missing every mutation path aborts without writing (and without deleting)
    instead of importing the agent package, so a curator thread can never
    half-apply a consolidation.
    """
    from runtime import data_provider

    writer = data_provider.get_skill_write_provider()
    if writer is None and method not in _provider_misses_logged:
        _provider_misses_logged.add(method)
        logger.debug(
            "curator: skill write provider is not registered; '{}' is skipped",
            method,
        )
    return writer


def _log_refresh_task_failure(task: asyncio.Future) -> None:
    if not task.cancelled() and task.exception() is not None:
        logger.debug("Curator: system prompt refresh task failed: {}", task.exception())


def _refresh_all_cached_system_prompts() -> None:
    """Rebuild and overwrite the cached system_prompt for every known session.

    After Curator consolidates/prunes skills the skill snapshot baked into
    each session's cached system_prompt is stale.  This forces a fresh
    build_system_prompt(session_id=sid) and writes it into both mem and db stores so the
    next turn picks up the new skill list immediately.
    """
    try:
        from runtime import state_register_mem, state_register_db

        provider = _resolve_provider("build_system_prompt")
        if provider is None:
            return

        # Rebuild skills_snapshot.json from disk FIRST so the subsequent
        # build_system_prompt(){scan_skills(use_cache=True)} cache-hit path
        # returns the freshly-consolidated/pruned skill list instead of the
        # stale snapshot (and so the on-disk file itself is kept in sync).
        from skills import build_skills_snapshot

        build_skills_snapshot()

        session_ids = state_register_db.get_all_session_ids()
        if not session_ids:
            logger.info("Curator: no sessions to refresh system_prompt for")
            return

        for sid in session_ids:
            new_prompt = provider.build_system_prompt(sid)
            state_register_mem.set_state(sid, "system_prompt", new_prompt)
            state_register_db.update_states(sid, {"system_prompt": new_prompt})

        logger.info("Curator: refreshed cached system_prompt for {} session(s)", len(session_ids))
    except Exception:
        logger.exception("Curator: failed to refresh cached system_prompts")


def _schedule_system_prompt_refresh() -> None:
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    if loop is not None and loop.is_running():
        task = asyncio.ensure_future(asyncio.to_thread(_refresh_all_cached_system_prompts))
        task.add_done_callback(_log_refresh_task_failure)
    else:
        asyncio.run(asyncio.to_thread(_refresh_all_cached_system_prompts))
