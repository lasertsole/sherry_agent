"""before_agent router: classify an interruption while a TaskFlow is active.

Registered in ``agent/middlewares/specs.py`` beside the existing TaskIntent
middleware. Runs ONCE per turn (before every before_model hook) and only acts
when the session has a non-terminal TaskFlow — short turns are a strict no-op.

Actions (see ``intent.py`` for the classifier and its fail-open contract):

* ``CANCEL``       — cancel every active flow via ``taskflow_cancel`` and inject
  a neutral internal notice so the transcript records the interruption.
* ``SOFT_PREEMPT`` / ``NEW`` — inject a transient internal steering message that
  names the active flow(s) and the user's new direction. It rides the next
  model call WITHOUT cancelling or pausing the executing step: the queued-input
  layer already serialises the turn, so this only informs the model.
* ``RESUME`` / ``CONTINUE`` — no-op; the existing HITL resume and normal message
  injection paths own these.

Everything is fail-open: any error logs and returns ``None``. The router can
never break a turn or drop a message.
"""

from __future__ import annotations

import asyncio
from typing import Any, override

from langchain.agents.middleware import AgentMiddleware, AgentState
from langchain_core.messages import BaseMessage, HumanMessage
from loguru import logger

from agent.middlewares.task_intent_router.intent import TurnIntent, classify_turn_intent
from agent.tools.subagent.registry.session_state import is_hitl_pending
from agent.tools.taskflow.registry import store_sqlite


#: Metadata marking the router's own injections as internal system directives
#: (never a real user turn) — mirrors task_intent._task_intent_message.
_ROUTER_METADATA = {"origin": "task_intent_router", "internal": True}

_CANCEL_NOTICE = (
    "[SYSTEM: user asked to stop the running task. The active TaskFlow "
    "was cancelled; acknowledge the stop and do not resume it unless asked.]"
)


def _last_human_text(messages: Any) -> tuple[str, BaseMessage | None]:
    """Return (text, message) of the last message when it is a genuine user turn.

    If the trailing message is an internal/system-directed injection (the
    router's own notice, task_intent steering, a subagent completion), this turn
    was already handled — return empty so a second model call in the same turn
    never re-classifies our own carrier (idempotence). Only a trailing human
    message is eligible for routing.
    """
    if not isinstance(messages, list) or not messages:
        return "", None
    for msg in reversed(messages):
        if not isinstance(msg, HumanMessage):
            continue
        meta = getattr(msg, "metadata", None) or {}
        if meta.get("internal") or meta.get("origin") in {"task_intent", "task_intent_router"}:
            # A system-injected carrier at the tail means this turn is already
            # steered; do not look past it to an older user message.
            return "", None
        content = getattr(msg, "content", "")
        if isinstance(content, str):
            return content, msg
        if isinstance(content, list):
            parts: list[str] = []
            for block in content:
                if isinstance(block, str):
                    parts.append(block)
                elif isinstance(block, dict) and block.get("type") == "text":
                    text = block.get("text")
                    if isinstance(text, str):
                        parts.append(text)
            return "\n".join(parts), msg
    return "", None


def _steering_text(intent: TurnIntent, texts: str, flow_ids: list[str]) -> str:
    """Build the internal steering message for a non-destructive interruption."""
    flows = ", ".join(flow_ids) or "the active task"
    if intent is TurnIntent.SOFT_PREEMPT:
        directive = "The user is changing direction (soft preemption)."
    else:  # NEW
        directive = "The user appears to be starting a new task."
    return (
        "[SYSTEM DIRECTIVE: TASK INTERRUPTION DETECTED]\n"
        f"{directive} A TaskFlow is currently active ({flows}). "
        "The executing step has NOT been interrupted. Weave the user's latest "
        "message into the current plan; if the task genuinely changed, finish "
        "or cancel the current flow deliberately before starting new work."
    )


class TaskIntentRouterMiddleware(AgentMiddleware):
    """``before_agent``: route interruptions during an active TaskFlow."""

    async def _route(self, state: AgentState, runtime: Any = None) -> dict[str, Any] | None:
        try:
            state = state if isinstance(state, dict) else {}
            session_id = str(state.get("session_id") or "").strip()
            if not session_id:
                return None

            text, _human = _last_human_text(state.get("messages"))
            if not text.strip():
                return None

            # Short turns (no active flow) are a strict no-op.
            flows = store_sqlite.get_active_flows_sync(session_id)
            if not flows:
                return None

            intent = classify_turn_intent(text, has_pending_hitl=is_hitl_pending(session_id))
            if intent in (TurnIntent.RESUME, TurnIntent.CONTINUE):
                return None

            flow_ids = [str(flow.get("flow_id") or "") for flow in flows]

            if intent is TurnIntent.CANCEL:
                cancelled: list[str] = []
                for flow_id in flow_ids:
                    if not flow_id:
                        continue
                    try:
                        result = await _cancel_flow(flow_id, session_id, text)
                        if result:
                            cancelled.append(flow_id)
                    except Exception:  # noqa: BLE001 - per-flow best effort
                        logger.warning(
                            "TaskIntentRouterMiddleware: failed to cancel flow {}", flow_id
                        )
                logger.info(
                    "TaskIntentRouterMiddleware: CANCEL for session {} ({} flow(s))",
                    session_id,
                    len(cancelled),
                )
                return {
                    "messages": [HumanMessage(content=_CANCEL_NOTICE, metadata=_ROUTER_METADATA)]
                }

            # SOFT_PREEMPT / NEW: advisory steering, no destructive action.
            logger.info(
                "TaskIntentRouterMiddleware: {} steering for session {}",
                intent.value,
                session_id,
            )
            return {
                "messages": [
                    HumanMessage(
                        content=_steering_text(intent, text, flow_ids),
                        metadata=_ROUTER_METADATA,
                    )
                ]
            }
        except Exception:
            logger.exception("TaskIntentRouterMiddleware: failed; continuing without routing")
            return None

    @override
    async def abefore_agent(self, state: AgentState, runtime: Any = None) -> dict[str, Any] | None:
        return await self._route(state, runtime)

    @override
    def before_agent(self, state: AgentState, runtime: Any = None) -> dict[str, Any] | None:
        """Sync path: run the async router only when no loop is already active."""
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            try:
                return asyncio.run(self._route(state, runtime))
            except Exception:
                logger.exception("TaskIntentRouterMiddleware (sync) failed; no-op")
                return None
        return None


async def _cancel_flow(flow_id: str, session_id: str, reason: str) -> bool:
    """Cancel one flow; return True only when the cancel actually took effect."""
    from agent.tools.taskflow.tools.taskflow_cancel import taskflow_cancel

    result = await taskflow_cancel.ainvoke(
        {
            "flow_id": flow_id,
            "reason": f"user interruption: {reason[:200]}",
            "session_id": session_id,
        }
    )
    text = result if isinstance(result, str) else str(result)
    if text.startswith("Error:") or "not found" in text.lower() or "terminal" in text.lower():
        return False
    return "cancelled" in text.lower()


__all__ = ["TaskIntentRouterMiddleware"]
