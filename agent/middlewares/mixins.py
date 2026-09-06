"""Mixin classes for agent middlewares (audit 1.1.9)."""

from typing import Any

from loguru import logger


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