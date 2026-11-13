"""Shared background-review agent scaffolding: the model/lane limit tool, graph state, agent builder and the cross-job in-flight lock."""

import asyncio
from collections.abc import Awaitable, Callable, Sequence
from typing import Any, override
from langchain.agents import create_agent
from langchain.agents.middleware import AgentMiddleware, AgentState
from langchain_core.messages import ToolMessage
from langchain_core.tools import BaseTool
from langgraph.prebuilt.tool_node import ToolCallRequest
from langgraph.types import Command
from loguru import logger
from runtime import StateKey, state_register_mem


# Lazy import to avoid circular dependency with IterationBudget
def _get_iteration_budget():
    from agent.middlewares import IterationBudget

    # Mirrors the main agent's wiring (agent/core.py): middleware lists take
    # instances, not classes.
    return IterationBudget(90)


_SKILL_REVIEW_LOCK_KEY = StateKey.SKILL_REVIEW_LOCK


_BACKGROUND_REVIEW_MEMORY_LOCK_KEY = StateKey.BACKGROUND_REVIEW_MEMORY_LOCK


# Keeps scheduled background-review coroutines referenced until they complete (asyncio
# holds only weak references to tasks).
_BACKGROUND_REVIEW_TASKS: set[asyncio.Task[None]] = set()


def _background_reviews_locked(session_id: str) -> bool:
    """True while a memory review or skill review is in flight."""
    return bool(
        state_register_mem.get_state(session_id, _BACKGROUND_REVIEW_MEMORY_LOCK_KEY, False)
        or state_register_mem.get_state(session_id, _SKILL_REVIEW_LOCK_KEY, False)
    )


class _BackgroundReviewLimitTool(AgentMiddleware):
    """Tool gate for background-review sub-agents.

    With no ``allowed_metadata_key`` (the default), a tool passes only when its
    metadata carries ``background_review: True`` — the memory review's rule (``memory``,
    ``knowledge``, ``question``, plan mode). With an explicit metadata key, a
    tool passes only when ``tool.metadata[key] is True`` (strict identity), so
    the compression todo fork can admit exactly the metadata-marked
    ``todowrite`` while every unmarked tool is rejected.

    The skills toolset is deliberately UNTAGGED: skill extraction is the
    plan-completion fork's job, and that fork binds its own explicit four-tool
    face (``_skill_review_tool_face``) instead of riding this gate.
    """

    def __init__(self, allowed_metadata_key: str | None = None) -> None:
        super().__init__()
        self._allowed_metadata_key = allowed_metadata_key

    @override
    async def awrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], Awaitable[ToolMessage | Command[Any]]],
    ) -> ToolMessage | Command[Any]:
        logger.bind(middleware=type(self).__name__).debug("awrap_tool_call hook fired")
        tool_name: str = request.tool_call.get("name", "unknown")

        if not self._is_allowed(request.tool):
            return ToolMessage(
                content=(
                    f"Tool [{tool_name}] is not allowed during a background review. "
                    "Execution has been skipped. Please reconsider your approach."
                ),
                tool_call_id=request.tool_call["id"],
                name=tool_name,
                status="error",
            )

        return await handler(request)

    @staticmethod
    def _is_background_review_allowed(tool: Any) -> bool:
        """Legacy metadata rule: only tools tagged ``background_review: True`` pass."""
        if tool is not None and isinstance(getattr(tool, "metadata", None), dict):
            return bool(tool.metadata.get("background_review", False))
        return False

    def _is_allowed(self, tool: Any) -> bool:
        if self._allowed_metadata_key is None:
            return self._is_background_review_allowed(tool)
        metadata = getattr(tool, "metadata", None)
        if isinstance(metadata, dict):
            return metadata.get(self._allowed_metadata_key) is True
        return False


class StateSchema(AgentState):
    """Agent state that preserves an ``session_id``."""

    session_id: str


async def _create_background_review_agent(
    system_prompt: str,
    allowed_metadata_key: str | None = None,
    tools: Sequence[BaseTool] | None = None,
):
    from agent.middlewares import ToolCallNormalize, ToolGuardrails
    from models import build_main_llm

    if tools is None:
        from agent import get_agent_tools

        agent_tools: Sequence[BaseTool] = get_agent_tools()
    else:
        agent_tools = list(tools)

    main_llm = build_main_llm()
    return create_agent(
        model=main_llm,
        state_schema=StateSchema,
        system_prompt=system_prompt,
        middleware=[
            _BackgroundReviewLimitTool(allowed_metadata_key=allowed_metadata_key),
            ToolCallNormalize(),
            ToolGuardrails(),
            _get_iteration_budget(),
        ],
        tools=list(agent_tools),
    )


def _session_tool_face(session_id: str) -> list[BaseTool]:
    """The tools the session's turn actually binds, in the same order.

    The provider's prefix cache is keyed on the request prefix, and the tool
    block is part of it — a review that binds a different tool list (or a
    different order) misses the cache at the very first token. Every main tool
    is always available now (the per-session tool selection was removed), so the
    review mirrors the process-wide list verbatim.
    """
    from agent import get_agent_tools

    return list(get_agent_tools())


def _build_session_model(session_id: str) -> Any:
    """A fresh main-model instance matching this session's last turn.

    Built from the CONFIGURATION the turn used (``current_session_model_config``)
    rather than a captured instance: the graph is rebuilt — and its transport
    pools closed — every turn, so a captured client can outlive its loop;
    a fresh instance with the same provider/model/thinking/temperature
    serialises the identical body and still hits the provider's prefix cache.
    No descriptor known (no main-chain call yet in this process, e.g. a session
    restored from disk right at a compaction) builds the env-default model.
    """
    from agent.middlewares.thinking_control.core import current_session_model_config
    from models import build_main_llm, build_main_llm_for_profile

    config = current_session_model_config(session_id) or {}
    override = config.get("override")
    temperature = config.get("temperature")
    thinking_kwargs = dict(config.get("thinking_kwargs") or {})
    custom_mapping = config.get("custom_mapping") or None
    if override:
        return build_main_llm_for_profile(
            provider=override.get("provider"),
            model=override["model"],
            api_key=override.get("api_key"),
            base_url=override.get("base_url"),
            temperature=temperature,
            custom_mapping=custom_mapping,
            **thinking_kwargs,
        )
    return build_main_llm(temperature=temperature, custom_mapping=custom_mapping, **thinking_kwargs)
