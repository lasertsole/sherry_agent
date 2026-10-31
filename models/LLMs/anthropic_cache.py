"""Explicit ``cache_control`` breakpoints for the Anthropic provider.

Anthropic is the one supported provider whose prompt cache is OPT-IN: nothing is
cached unless the request marks a prefix with ``cache_control``. Sherry's whole
prompt architecture — a frozen system prompt, a session-stable tool face, live
state pushed down into message-level reminders — was designed for providers that
cache automatically, so on Anthropic it produced zero cache hits.

This module mounts a thin ``BaseChatModel`` wrapper around ``ChatAnthropic`` that
adds three breakpoints to the OUTGOING request, using the order Anthropic builds
its prefix in (tools → system → messages):

===============  ==========================================  ====================
Breakpoint       Placed on                                   Why it is stable
===============  ==========================================  ====================
BP1 (system)     last text block of the ``SystemMessage``    frozen per session
BP2 (tools)      the last converted tool definition          session tool face
BP3 (history)    the last real history message (AI or tool)  everything after it
                 seen when scanning back past human carriers is this turn's input
===============  ==========================================  ====================

Why BP3 is not "the last message": ``DynamicReminderMiddleware`` injects a
``<system-reminder>`` block, and the workspace notice can splice another, right
before the turn's user message — both change between turns. A breakpoint there
would miss every time. The last message BEFORE that block is the previous turn's
settled tail, which is exactly the prefix worth reusing; the reminder and the new
user text stay uncached input.

Everything is fail-open and non-mutating: the caller's message objects and the
bound ``tools`` list are never touched (the request is built from copies), an
injection error returns the original request untouched, and the wrapper is only
mounted for the ``anthropic`` provider — every other provider's requests are
byte-identical to before (``SHERRY_ANTHROPIC_CACHE=0`` makes this a pure
pass-through even there).
"""

from __future__ import annotations

from typing import Any
from collections.abc import AsyncIterator, Iterator, Mapping, Sequence

from langchain_core.callbacks import (
    AsyncCallbackManagerForLLMRun,
    CallbackManagerForLLMRun,
)
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import BaseMessage, SystemMessage
from langchain_core.outputs import ChatGenerationChunk, ChatResult
from loguru import logger
from pydantic import Field

from config.features import ANTHROPIC_CACHE

__all__ = [
    "AnthropicCacheControlChatModel",
    "cache_control_value",
    "inject_breakpoints",
    "is_anthropic_provider",
    "maybe_wrap_anthropic_cache",
]

#: The providers this wrapper is mounted for. Bedrock is excluded on purpose:
#: its Claude models go through ``langchain-aws``, whose cache handling differs.
_ANTHROPIC_PROVIDERS = frozenset({"anthropic"})


def is_anthropic_provider(provider: str | None) -> bool:
    """True when *provider* is the native Anthropic integration."""
    return str(provider or "").strip().lower() in _ANTHROPIC_PROVIDERS


def cache_control_value(
    *, ttl: str | None = None, enabled: bool | None = None
) -> dict[str, str] | None:
    """The ``cache_control`` payload for this deployment, or ``None`` when off."""
    if enabled is None:
        enabled = bool(ANTHROPIC_CACHE["enabled"])
    if not enabled:
        return None
    effective_ttl = (ttl or str(ANTHROPIC_CACHE["ttl"] or "short")).strip().lower()
    if effective_ttl == "long":
        return {"type": "ephemeral", "ttl": "1h"}
    return {"type": "ephemeral"}


def maybe_wrap_anthropic_cache(model: Any, provider: str | None) -> Any:
    """Wrap *model* for Anthropic only; every other provider passes through (C1/C5)."""
    if not is_anthropic_provider(provider):
        return model
    if not ANTHROPIC_CACHE["enabled"]:
        return model
    if isinstance(model, AnthropicCacheControlChatModel):
        return model
    try:
        return AnthropicCacheControlChatModel(inner=model)
    except Exception:  # noqa: BLE001 - a wrapper failure must never break model building
        logger.warning(
            "anthropic cache: wrapper mount failed; using the bare client", exc_info=True
        )
        return model


def _count_existing(messages: Sequence[BaseMessage], tools: Any) -> int:
    """Breakpoints the caller already placed (so we never double up)."""
    total = 0
    for message in messages:
        content = getattr(message, "content", None)
        if isinstance(content, list):
            total += sum(
                1 for block in content if isinstance(block, dict) and block.get("cache_control")
            )
    if isinstance(tools, (list, tuple)):
        total += sum(1 for tool in tools if isinstance(tool, dict) and tool.get("cache_control"))
    return total


def _with_breakpoint(message: BaseMessage, value: dict[str, str]) -> BaseMessage | None:
    """A COPY of *message* whose last text block carries ``cache_control``.

    ``None`` when the message has no text block that may carry one (an assistant
    turn that is nothing but tool calls). The caller then keeps scanning back:
    a breakpoint further into the history is still a valid, reusable prefix.
    """
    content = message.content
    if isinstance(content, str):
        if not content.strip():
            return None
        blocks: list[Any] = [{"type": "text", "text": content, "cache_control": value}]
    elif isinstance(content, list):
        blocks = list(content)
        target: int | None = None
        for index in range(len(blocks) - 1, -1, -1):
            block = blocks[index]
            if (
                isinstance(block, dict)
                and block.get("type") == "text"
                and not block.get("cache_control")
            ):
                target = index
                break
        if target is None:
            return None
        blocks[target] = {**blocks[target], "cache_control": value}
    else:
        return None
    return message.model_copy(update={"content": blocks})


def _convert_tools(tools: Any) -> list[Any] | None:
    """Convert a bound tool list to Anthropic definitions, or ``None`` on failure."""
    from langchain_anthropic.chat_models import convert_to_anthropic_tool

    try:
        return [convert_to_anthropic_tool(tool) for tool in tools]
    except Exception:  # noqa: BLE001 - BP2 is optional; BP1/BP3 still apply
        logger.debug("anthropic cache: tool conversion failed; skipping the tool breakpoint")
        return None


def inject_breakpoints(
    messages: Sequence[BaseMessage],
    tools: Any,
    *,
    value: dict[str, str] | None = None,
    max_breakpoints: int | None = None,
) -> tuple[list[BaseMessage], Any, int]:
    """Return ``(messages, tools, placed)`` with up to *max_breakpoints* marks.

    The inputs are never mutated. A caller that already placed its own
    breakpoints keeps them: they count against the budget and, once it is spent,
    nothing is added.
    """
    control = value if value is not None else cache_control_value()
    if control is None:
        return list(messages), tools, 0
    limit = int(ANTHROPIC_CACHE["max_breakpoints"] if max_breakpoints is None else max_breakpoints)
    if limit <= 0:
        return list(messages), tools, 0
    budget = limit - _count_existing(messages, tools)
    if budget <= 0:
        return list(messages), tools, 0

    updated = list(messages)
    placed = 0

    # BP1 — the system prompt. Anthropic's prefix order is tools → system →
    # messages, so this one breakpoint caches the tools as well.
    for index in range(len(updated) - 1, -1, -1):
        if not isinstance(updated[index], SystemMessage):
            continue
        marked = _with_breakpoint(updated[index], control)
        if marked is not None:
            updated[index] = marked
            placed += 1
        break

    # BP2 — the last tool definition. Only useful when the system prompt changes
    # mid-session (a thinking switch or an agent-config edit), where the tools
    # alone remain a reusable prefix.
    if placed < budget and isinstance(tools, (list, tuple)) and tools:
        converted = _convert_tools(tools)
        if converted:
            converted[-1] = {**converted[-1], "cache_control": control}
            tools = converted
            placed += 1

    # BP3 — the settled history tail, scanning back PAST the current turn's
    # human carriers (the reminder block, the workspace notice, the user text).
    for index in range(len(updated) - 1, -1, -1):
        if placed >= budget:
            break
        message = updated[index]
        if isinstance(message, SystemMessage) or getattr(message, "type", "") == "human":
            continue
        marked = _with_breakpoint(message, control)
        if marked is None:
            continue
        updated[index] = marked
        placed += 1
        break

    return updated, tools, placed


class AnthropicCacheControlChatModel(BaseChatModel):
    """Delegate to an inner chat model, marking Anthropic cache breakpoints.

    A genuine ``BaseChatModel`` (so ``create_agent``, ``.bind_tools`` and
    ``.with_structured_output`` keep working) whose only job is to rewrite each
    outgoing request before delegating. Everything the inner model exposes for
    identity — ``_llm_type``, ``_identifying_params`` — is mirrored, so the
    thinking-control profile fingerprint does not change when the wrapper is
    mounted.
    """

    inner: BaseChatModel = Field(exclude=True)

    @property
    def _llm_type(self) -> str:
        return f"anthropic-cache+{self.inner._llm_type}"

    @property
    def _identifying_params(self) -> Mapping[str, Any]:
        return {
            "inner_type": self.inner._llm_type,
            **dict(getattr(self.inner, "_identifying_params", {}) or {}),
        }

    @property
    def lc_attributes(self) -> Mapping[str, Any]:
        return self._identifying_params

    def with_structured_output(self, schema: Any = None, **kwargs: Any) -> Any:
        """Delegate to the inner model so its structured-output strategy survives.

        ``ChatAnthropic`` overrides this method with provider-specific tool /
        json-schema strategies; inheriting the generic ``BaseChatModel``
        implementation would silently replace them. The price is that the
        returned runnable calls the inner model directly and carries no
        breakpoints — a structured-output call is one request either way, and
        correctness outranks a mark.
        """
        delegate = getattr(self.inner, "with_structured_output", None)
        if callable(delegate):
            return delegate(schema, **kwargs)
        return super().with_structured_output(schema, **kwargs)

    # -- request rewriting --------------------------------------------------
    def _prepare(
        self, messages: Sequence[BaseMessage], kwargs: dict[str, Any]
    ) -> tuple[list[BaseMessage], dict[str, Any]]:
        """Messages + kwargs for the inner call, marked or untouched (fail-open)."""
        try:
            marked, tools, placed = inject_breakpoints(messages, kwargs.get("tools"))
        except Exception:  # noqa: BLE001 - C5: injection must never break generation
            logger.warning("anthropic cache: breakpoint injection failed; sending unchanged")
            return list(messages), kwargs
        if not placed:
            return list(messages), kwargs
        forwarded = dict(kwargs)
        if tools is not kwargs.get("tools"):
            forwarded["tools"] = tools
        return marked, forwarded

    # -- Generation ---------------------------------------------------------
    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: CallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        prepared, forwarded = self._prepare(messages, kwargs)
        return self.inner._generate(prepared, stop=stop, run_manager=run_manager, **forwarded)

    async def _agenerate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: AsyncCallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        prepared, forwarded = self._prepare(messages, kwargs)
        return await self.inner._agenerate(
            prepared, stop=stop, run_manager=run_manager, **forwarded
        )

    # -- Streaming ----------------------------------------------------------
    def _stream(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: CallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> Iterator[ChatGenerationChunk]:
        prepared, forwarded = self._prepare(messages, kwargs)
        yield from self.inner._stream(prepared, stop=stop, run_manager=run_manager, **forwarded)

    async def _astream(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: AsyncCallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> AsyncIterator[ChatGenerationChunk]:
        prepared, forwarded = self._prepare(messages, kwargs)
        async for chunk in self.inner._astream(
            prepared, stop=stop, run_manager=run_manager, **forwarded
        ):
            yield chunk
