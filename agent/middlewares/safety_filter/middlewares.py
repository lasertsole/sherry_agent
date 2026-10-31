"""The three safety-filter middlewares.

* :class:`InputSafetyFilter` — keyword rules on the turn's user message, in
  ``wrap_model_call`` so a ``block`` hit returns a refusal WITHOUT calling the
  provider (no tokens, no latency).
* :class:`LLMModerationMiddleware` — optional review by a configured model,
  inner to the keyword filter (a keyword block never pays for a review call).
* :class:`OutputSafetyFilter` — the model's finished answer, in ``after_model``.
  Registered so its hook runs BEFORE ``MessagePersistence`` writes the row: the
  replaced text is what the transcript stores.

A streaming answer never reaches the output middleware: the stream path scrubs
each chunk with :class:`~.stream_guard.StreamTextGuard` before emitting it, so
the client and the persisted row agree on what was cut.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from typing import Any, override

from langchain.agents.middleware import AgentMiddleware, AgentState, ModelRequest
from langchain.agents.middleware.types import ModelResponse
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage
from langgraph.typing import ContextT
from loguru import logger

from .config import llm_moderation_active, safety_filter_config, safety_enabled
from .notify import notify_safety
from .rules import apply_replacements, scan

__all__ = ["InputSafetyFilter", "LLMModerationMiddleware", "OutputSafetyFilter"]

#: Wall clock a review call may take before the filter fails open. A safety
#: check that stalls the turn is worse than none: the provider's own alignment
#: is still in the loop.
_MODERATION_TIMEOUT_S = 20.0

_MODERATION_SYSTEM = (
    "You are a content safety reviewer. Decide whether the MESSAGE below is "
    "safe to show during a live stream: political sensitivity, illegal or "
    "harmful instructions, or anything unsuitable for a public audience. "
    'Reply with JSON only: {"safe": true, "reason": "<short reason>"}.'
)


def _session_id(state: Any) -> str:
    if isinstance(state, dict):
        value = state.get("session_id")
    else:
        value = getattr(state, "session_id", None)
    return str(value or "")


def _messages(state: Any) -> list[Any]:
    if isinstance(state, dict):
        value = state.get("messages")
    else:
        value = getattr(state, "messages", None)
    return value or []


def _internal(message: Any) -> bool:
    metadata = getattr(message, "additional_kwargs", None) or {}
    if not isinstance(metadata, dict):
        return False
    return bool(metadata.get("internal") or metadata.get("provenance"))


def _last_human(state: Any) -> HumanMessage | None:
    """The turn's user message (the last non-internal human row)."""
    for message in reversed(_messages(state)):
        if isinstance(message, HumanMessage) and not _internal(message):
            return message
    return None


def _last_ai(state: Any) -> AIMessage | None:
    for message in reversed(_messages(state)):
        if isinstance(message, AIMessage):
            return message
    return None


class InputSafetyFilter(AgentMiddleware):
    """Keyword rules on the user's message; ``block`` never reaches the model."""

    @override
    async def awrap_model_call(
        self,
        request: ModelRequest[ContextT],
        handler: Callable[[ModelRequest[ContextT]], Awaitable[ModelResponse[Any]]],
    ) -> ModelResponse[Any] | AIMessage:
        if not safety_enabled():
            return await handler(request)
        session_id = _session_id(request.state)
        message = _last_human(request.state)
        if message is None or not message.content:
            return await handler(request)
        hits = scan(str(message.content), scope="input")
        if not hits:
            return await handler(request)
        blocked = [hit for hit in hits if hit.action == "block"]
        notify_safety(
            session_id,
            layer="input_keyword",
            action="block" if blocked else "warn",
            rule_count=len(hits),
            message=(
                f"输入命中 {len(blocked)} 条拦截规则，已拦截"
                if blocked
                else f"输入命中 {len(hits)} 条安全规则（已放行）"
            ),
            snippet=str(message.content)[:50],
        )
        if blocked:
            refusal = safety_filter_config()["safe_refusal_message"]
            return AIMessage(content=refusal)
        return await handler(request)


class LLMModerationMiddleware(AgentMiddleware):
    """Optional model review of the user's message (off unless fully configured)."""

    @override
    async def awrap_model_call(
        self,
        request: ModelRequest[ContextT],
        handler: Callable[[ModelRequest[ContextT]], Awaitable[ModelResponse[Any]]],
    ) -> ModelResponse[Any] | AIMessage:
        if not llm_moderation_active():
            return await handler(request)
        message = _last_human(request.state)
        if message is None or not message.content:
            return await handler(request)
        text = str(message.content)
        settings = safety_filter_config()
        if settings["llm_moderation_mode"] == "on_keyword_hit" and not scan(text, scope="input"):
            return await handler(request)
        safe, reason = await _review(text, settings["llm_moderation_model"])
        if safe is None:
            # Unreachable / unparseable review: fail open and say so once.
            logger.warning("safety filter: LLM moderation unavailable, allowing the turn")
            return await handler(request)
        if safe:
            return await handler(request)
        session_id = _session_id(request.state)
        notify_safety(
            session_id,
            layer="llm_moderation",
            action="block",
            rule_count=1,
            message=f"LLM 审核判定为不安全，已拦截：{reason}"[:200],
            snippet=text[:50],
        )
        return AIMessage(content=settings["safe_refusal_message"])


class OutputSafetyFilter(AgentMiddleware):
    """Keyword rules on the finished answer; replaces or blocks the stored row."""

    @override
    async def aafter_model(self, state: AgentState, runtime: Any = None) -> dict[str, Any] | None:
        if not safety_enabled():
            return None
        message = _last_ai(state)
        if message is None or not isinstance(message.content, str) or not message.content:
            return None
        hits = scan(message.content, scope="output")
        if not hits:
            return None
        session_id = _session_id(state)
        blocked = [hit for hit in hits if hit.action == "block"]
        settings = safety_filter_config()
        if blocked:
            content = settings["safe_replacement_message"]
        elif any(hit.action == "replace" for hit in hits):
            content = apply_replacements(message.content, hits)
        else:
            content = message.content
        notify_safety(
            session_id,
            layer="output_keyword",
            action="block" if blocked else ("replace" if content != message.content else "warn"),
            rule_count=len(hits),
            message=(
                f"输出命中 {len(blocked)} 条拦截规则，已整体替换"
                if blocked
                else (
                    "输出部分内容已按规则替换"
                    if content != message.content
                    else f"输出命中 {len(hits)} 条安全规则（已放行）"
                )
            ),
            snippet=message.content[:50],
        )
        if content == message.content:
            return None
        # Same id → the store upserts the row instead of appending a new one.
        return {"messages": [AIMessage(content=content, id=message.id)]}


async def _review(text: str, model_name: str) -> tuple[bool | None, str]:
    """Ask the configured model whether ``text`` is safe.

    Returns ``(None, reason)`` when the review could not run (no model, request
    failure, timeout, unparseable answer) — the caller then fails open. The
    review uses the auxiliary family's provider/credentials with the configured
    model name, so one name identifies the reviewer.
    """
    name = str(model_name or "").strip()
    if not name:
        return None, "no review model configured"
    try:
        from models.LLMs.auxiliary_llm import build_auxiliary_llm

        llm = build_auxiliary_llm(temperature=0, model_name=name)
        messages: list[BaseMessage] = [
            HumanMessage(content=f"{_MODERATION_SYSTEM}\n\nMESSAGE:\n{text[:4000]}")
        ]
        reply = await asyncio.wait_for(llm.ainvoke(messages), timeout=_MODERATION_TIMEOUT_S)
        raw = reply.content if isinstance(reply.content, str) else json.dumps(reply.content)
        verdict = _parse_verdict(raw)
        if verdict is None:
            return None, "unparseable review answer"
        return verdict
    except TimeoutError:
        return None, "review timed out"
    except Exception as exc:  # noqa: BLE001 - the review must never break a turn
        logger.warning("safety filter: review call failed: {}", exc)
        return None, "review failed"


def _parse_verdict(raw: str) -> tuple[bool, str] | None:
    """``{"safe": bool, "reason": str}`` out of the model's reply (tolerates prose)."""
    start = raw.find("{")
    end = raw.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        data = json.loads(raw[start : end + 1])
    except ValueError:
        return None
    if not isinstance(data, dict) or not isinstance(data.get("safe"), bool):
        return None
    return bool(data["safe"]), str(data.get("reason") or "")[:200]
