"""Per-session token budget cap.

The client sets an optional cap (``PUT /sessions/budget``). This middleware:

* short-circuits a model call with a terminal ``AIMessage`` once the session's
  accumulated usage has reached the cap;
* accumulates every model response's provider-reported token usage across
  turns (``TOKEN_BUDGET_USED``), plus an ESTIMATED cost (``TOKEN_BUDGET_COST``)
  from, in order: provider-returned cost, the user's ``.models`` price table,
  the local litellm cache, the table's ``_default``;
* pushes ``budget_warning`` at the configured threshold and ``budget_limited``
  once, when the cap is first reached.

An absent/0 cap is a pure pass-through. Token counts are provider-reported and
therefore exact; cost is an estimate and never drives the cap.
"""

from __future__ import annotations

import asyncio
import threading
from typing import Any, override
from collections.abc import Awaitable, Callable

from langchain_core.messages import AIMessage
from langchain.agents.middleware import AgentMiddleware, AgentState
from langchain.agents.middleware.types import (
    ExtendedModelResponse,
    ModelRequest,
    ModelResponse,
    ResponseT,
)
from langgraph.typing import ContextT
from loguru import logger

from config.features import MODEL_PRICING
from config.features.infra_side.model_pricing import resolve_model_pricing
from runtime import StateKey, state_register_db, state_register_mem

BUDGET_LIMITED_EVENT = "budget_limited"
BUDGET_WARNING_EVENT = "budget_warning"

#: Guards a same-session read-modify-write of the usage counters. Model calls
#: are serial per turn, but a sync and an async path can overlap in tests/tools.
_BUDGET_LOCK = threading.Lock()

#: Sessions already warned at the threshold (process-local; reset by the API).
_WARNED: set[str] = set()


def reset_budget_notice(session_id: str) -> None:
    """Forget the warning latch so a reset budget can warn again."""
    _WARNED.discard(session_id)


def _read_state(session_id: str, key: str, default: Any) -> Any:
    value = state_register_mem.get_state(session_id, key, None)
    if value is not None:
        return value
    value = state_register_db.get_state(session_id, key, None)
    if value is not None:
        # Rehydrate the hot tier for the next agent-side read.
        state_register_mem.set_state(session_id, key, value)
        return value
    return default


def _write_state(session_id: str, key: str, value: Any) -> None:
    """Dual-write a counter (mem for the hot path, DB for restart durability)."""
    state_register_mem.set_state(session_id, key, value)
    state_register_db.set_state(session_id, key, value)


def _delete_state(session_id: str, key: str) -> None:
    state_register_mem.delete_state(session_id, key)
    state_register_db.delete_state(session_id, key)


def _session_id(state: AgentState) -> str:
    return str(state.get("session_id") or "").strip()


def _extract_ai_message(result: Any) -> AIMessage | None:
    """The AIMessage in a handler return (bare / ModelResponse / Extended)."""
    if isinstance(result, AIMessage):
        return result
    body = getattr(result, "result", None)
    if isinstance(body, (list, tuple)):
        for candidate in reversed(list(body)):
            message = _extract_ai_message(candidate)
            if message is not None:
                return message
    inner = getattr(result, "model_response", None)
    if inner is not None and inner is not result:
        message = _extract_ai_message(inner)
        if message is not None:
            return message
    # Duck-typed AIMessage (some providers/tests return a small object).
    if hasattr(result, "usage_metadata") and hasattr(result, "content"):
        return result
    return None


def _model_name(ai_msg: AIMessage) -> str:
    metadata = getattr(ai_msg, "response_metadata", None) or {}
    for key in ("model_name", "model"):
        value = metadata.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    usage = getattr(ai_msg, "usage_metadata", None) or {}
    value = usage.get("model_name") if isinstance(usage, dict) else None
    if isinstance(value, str) and value.strip():
        return value.strip()
    name = getattr(ai_msg, "name", None)
    return name.strip() if isinstance(name, str) else ""


def _provider_cost(usage: dict, ai_msg: AIMessage) -> float | None:
    """Provider-returned USD cost (OpenRouter-style) if present."""
    sources = [usage]
    metadata = getattr(ai_msg, "response_metadata", None)
    if isinstance(metadata, dict):
        sources.append(metadata)
    for source in sources:
        for field in ("cost", "total_cost", "estimated_cost", "total_price"):
            value = source.get(field)
            if isinstance(value, (int, float)) and not isinstance(value, bool) and value >= 0:
                return float(value)
        prompt_cost = source.get("prompt_cost")
        completion_cost = source.get("completion_cost")
        if (
            isinstance(prompt_cost, (int, float))
            and not isinstance(prompt_cost, bool)
            and isinstance(completion_cost, (int, float))
            and not isinstance(completion_cost, bool)
        ):
            return float(prompt_cost) + float(completion_cost)
    return None


def _cache_tokens(ai_msg: AIMessage, usage: dict) -> tuple[int, int, bool]:
    """``(cache_read, cache_write, input_includes_cached)`` from one response.

    LangChain standardizes cache counts into ``input_token_details``
    (``cache_read`` for a prefix read, ``cache_creation`` for a write); a raw
    Anthropic usage dict in ``response_metadata`` is the fallback for providers
    that predate the detail map.

    ``input_includes_cached`` matters for the arithmetic, not for display:
    OpenAI's ``input_tokens`` already CONTAINS the cached ones, so they must be
    subtracted before the uncached rate is applied; Anthropic's excludes them,
    so subtracting would under-count the bill.
    """
    read = 0
    write = 0
    details = usage.get("input_token_details")
    if isinstance(details, dict):
        try:
            read = int(details.get("cache_read") or 0)
            write = int(details.get("cache_creation") or 0)
        except (TypeError, ValueError):
            read = write = 0
    raw_usage: dict[str, Any] = {}
    metadata = getattr(ai_msg, "response_metadata", None)
    if isinstance(metadata, dict):
        candidate = metadata.get("usage")
        if isinstance(candidate, dict):
            raw_usage = candidate
    if read <= 0:
        try:
            read = int(raw_usage.get("cache_read_input_tokens") or 0)
        except (TypeError, ValueError):
            read = 0
    if write <= 0:
        try:
            write = int(raw_usage.get("cache_creation_input_tokens") or 0)
        except (TypeError, ValueError):
            write = 0
    # OpenAI-shaped usage reports ``prompt_tokens`` (cached included) plus a
    # ``*_details.cached_tokens`` subset; Anthropic reports ``input_tokens``
    # that already excludes the cached portion.
    includes_cached = "prompt_tokens" in raw_usage or "prompt_tokens_details" in raw_usage
    return max(0, read), max(0, write), includes_cached


def _cost_delta(ai_msg: AIMessage, usage: dict, input_tokens: int, output_tokens: int) -> float:
    direct = _provider_cost(usage, ai_msg)
    if direct is not None:
        return direct
    pricing = resolve_model_pricing(_model_name(ai_msg))
    cache_read, cache_write, includes_cached = _cache_tokens(ai_msg, usage)
    if not cache_read and not cache_write:
        return (input_tokens * pricing["input"] + output_tokens * pricing["output"]) / 1_000_000
    uncached = max(0, input_tokens - cache_read - cache_write) if includes_cached else input_tokens
    # A table without the cache prices prices those tokens at the input rate,
    # which is precisely the pre-cache estimate: no invented discount.
    cache_read_price = pricing.get("cache_read", pricing["input"])
    cache_write_price = pricing.get("cache_write", pricing["input"])
    return (
        uncached * pricing["input"]
        + cache_read * cache_read_price
        + cache_write * cache_write_price
        + output_tokens * pricing["output"]
    ) / 1_000_000


def _notify(session_id: str, event: str, content: dict[str, Any]) -> None:
    """Best-effort WS push; a missing loop/socket is a no-op."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    loop.create_task(_push_event(session_id, event, content))


async def _push_event(session_id: str, event: str, content: dict[str, Any]) -> None:
    try:
        from runtime.session.ws_push import broadcast_to_session

        await broadcast_to_session(
            session_id,
            {"event": event, "session_id": session_id, "content": content},
            ensure_ascii=False,
        )
    except Exception as exc:  # noqa: BLE001 - a push failure must never break a model call
        logger.debug("budget push skipped for {} ({}): {}", session_id, event, exc)


class TokenBudget(AgentMiddleware):
    """Enforce an optional per-session token cap and accumulate usage."""

    @staticmethod
    def _pre_call(state: AgentState) -> AIMessage | None:
        session_id = _session_id(state)
        if not session_id:
            return None
        cap = int(_read_state(session_id, StateKey.TOKEN_BUDGET_CAP, 0) or 0)
        if cap <= 0:
            return None
        used = int(_read_state(session_id, StateKey.TOKEN_BUDGET_USED, 0) or 0)
        if used < cap:
            return None

        if not bool(_read_state(session_id, StateKey.TOKEN_BUDGET_LIMITED, False)):
            _write_state(session_id, StateKey.TOKEN_BUDGET_LIMITED, True)
            _notify(session_id, BUDGET_LIMITED_EVENT, {"used": used, "cap": cap})
        return AIMessage(
            content=(
                f"[SYSTEM] Token budget exhausted ({used}/{cap} tokens). "
                "Stop calling tools and provide a concise summary of what was "
                "accomplished. The operator can reset or raise the budget in the "
                "session controls."
            )
        )

    @staticmethod
    def _post_call(state: AgentState, result: Any) -> None:
        session_id = _session_id(state)
        if not session_id:
            return
        ai_msg = _extract_ai_message(result)
        if ai_msg is None:
            return
        usage = getattr(ai_msg, "usage_metadata", None)
        if not isinstance(usage, dict):
            return
        try:
            input_tokens = int(usage.get("input_tokens") or 0)
            output_tokens = int(usage.get("output_tokens") or 0)
        except (TypeError, ValueError):
            return
        total = input_tokens + output_tokens
        if total <= 0:
            return

        with _BUDGET_LOCK:
            used = int(_read_state(session_id, StateKey.TOKEN_BUDGET_USED, 0) or 0) + total
            cost = float(_read_state(session_id, StateKey.TOKEN_BUDGET_COST, 0.0) or 0.0)
            cost += _cost_delta(ai_msg, usage, input_tokens, output_tokens)
            _write_state(session_id, StateKey.TOKEN_BUDGET_USED, used)
            _write_state(session_id, StateKey.TOKEN_BUDGET_COST, round(cost, 6))

            cap = int(_read_state(session_id, StateKey.TOKEN_BUDGET_CAP, 0) or 0)
            if cap <= 0:
                return
            limited = bool(_read_state(session_id, StateKey.TOKEN_BUDGET_LIMITED, False))
            if used >= cap:
                if not limited:
                    _write_state(session_id, StateKey.TOKEN_BUDGET_LIMITED, True)
                    _notify(session_id, BUDGET_LIMITED_EVENT, {"used": used, "cap": cap})
                return
            threshold = float(cap) * float(MODEL_PRICING["budget_warn_threshold"])
            if used >= threshold and session_id not in _WARNED:
                _WARNED.add(session_id)
                _notify(
                    session_id,
                    BUDGET_WARNING_EVENT,
                    {
                        "used": used,
                        "cap": cap,
                        "threshold": MODEL_PRICING["budget_warn_threshold"],
                    },
                )

    @override
    def wrap_model_call(
        self,
        request: ModelRequest[ContextT],
        handler: Callable[[ModelRequest[ContextT]], ModelResponse[ResponseT]],
    ) -> ModelResponse[ResponseT] | AIMessage | ExtendedModelResponse[ResponseT]:
        terminal = self._pre_call(request.state)
        if terminal is not None:
            return terminal
        result = handler(request)
        self._post_call(request.state, result)
        return result

    @override
    async def awrap_model_call(
        self,
        request: ModelRequest[ContextT],
        handler: Callable[[ModelRequest[ContextT]], Awaitable[ModelResponse[ResponseT]]],
    ) -> ModelResponse[ResponseT] | AIMessage | ExtendedModelResponse[ResponseT]:
        terminal = self._pre_call(request.state)
        if terminal is not None:
            return terminal
        result = await handler(request)
        self._post_call(request.state, result)
        return result
