"""Task 7 — before_model middleware: drain subagent-completion steering injections.

The announce pipeline (plan tasks 5/6) queues busy-session completion messages
into the per-session ``SteeringQueue`` (memory + SQLite). This middleware is the
parent-turn ingestion point: at ``before_model`` it rehydrates + drains the
session's queue and returns ``{"messages": [carrier, ...]}`` so the
``add_messages`` reducer injects the rebuilt ``HumanMessage`` carriers right
before the next model call (same graph, same checkpoint persistence).

Design guarantees:

- blank/missing ``session_id`` → no-op (never break the turn);
- empty queue → no-op;
- ``drain`` marks SQLite rows CONSUMED → re-injection impossible;
- checkpoint persistence makes HITL-resume replays safe (queued items are
  drained exactly once, before the resumed turn's model call);
- every failure is swallowed (log + return None) — the drain must never
  break the parent turn.

The sync ``before_model`` is best-effort: production turns are async-only
(``astream``/``ainvoke``). Inside a running event loop it is a debug-logged
no-op; otherwise it runs the async impl via ``asyncio.run``.
"""

from __future__ import annotations

import asyncio
from typing import Any

from langchain.agents.middleware import AgentMiddleware
from loguru import logger

from agent.tools.subagent.announce.steering_queue import drain, rehydrate

__all__ = ["SubagentCompletionDrainMiddleware"]


def _is_internal_completion(msg: Any) -> bool:
    """True when ``msg`` carries the frozen task-4 completion metadata contract.

    Contract (completion_message.py): messages carry
    ``metadata = {"internal": True, "provenance": "subagent_completion",
    "run_id": ..., "status": ...}`` on ``BaseMessage.metadata`` (NOT
    additional_kwargs). Only ``internal`` + ``provenance`` are load-bearing
    here: rehydrated carriers cannot restore ``status`` (task 3 API gap).
    """
    meta = getattr(msg, "metadata", None) or {}
    return bool(meta.get("internal")) and meta.get("provenance") == "subagent_completion"


class SubagentCompletionDrainMiddleware(AgentMiddleware):
    """Inject queued subagent-completion steering messages before a model call.

    Registered in ``agent/core.py`` immediately AFTER ``ToolCallNormalize`` so
    the injected messages bypass the sanitize rewrite on the injection turn.
    """

    async def abefore_model(self, state, runtime=None):
        """Drain the session queue; return ``{"messages": [...]}`` or ``None``.

        ``item.message`` is the rebuilt task-4 carrier ``HumanMessage`` carrying
        proper completion metadata — injected directly, never reconstructed.
        """
        try:
            key = ""
            if isinstance(state, dict):
                key = state.get("session_id") or ""
            if not str(key).strip():
                logger.debug("completion drain: no session_id in state; skipping")
                return None
            await rehydrate(key)
            items = await drain(key)
            if not items:
                return None
            logger.info(
                "completion drain: injecting {} subagent completion message(s) into session {}",
                len(items),
                key,
            )
            return {"messages": [item.message for item in items]}
        except Exception:
            logger.exception("completion drain failed; continuing turn without injection")
            return None

    def before_model(self, state, runtime=None):
        """Sync best-effort path (production main agent streams async-only)."""
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            try:
                return asyncio.run(self.abefore_model(state, runtime))
            except Exception:
                logger.exception("completion drain (sync) failed; continuing without injection")
                return None
        logger.debug("completion drain: running loop detected; async-only path skips")
        return None
