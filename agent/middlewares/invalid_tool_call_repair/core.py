"""InvalidToolCallRepairMiddleware — salvage tool calls whose JSON args don't parse.

Why this exists (SWE-bench run ``20261017_003529``): the model emitted a
``terminal`` call whose ``arguments`` string was not valid JSON
(``{"commands": cd $(pwd) && git log --oneline -3}`` — the shell command was
never quoted). ``langchain_core``'s ``default_tool_parser`` filed the call into
``AIMessage.invalid_tool_calls`` (raw string as ``args``, ``error=None``) and
left ``tool_calls`` empty. The agent loop routes on ``tool_calls`` only, so the
message looked like a call-less final answer — the turn ended silently, the
model never learned, and the instance produced zero edits while the model's own
text said it would continue.

This middleware sits at the innermost ``wrap_model_call`` seam (registered LAST
in ``agent/core.py::_build_middlewares``), so the graph state, the checkpointer
and MessagePersistence all see the REPAIRED message — the persisted row and the
routing decision can never diverge.

Two stages, both fail-open:

* **Repair** — syntax-only (``repair.py``). The recovered call keeps its original
  ``id`` and ``name``, so the ``tool_start`` frame the client already received
  (projected from the raw stream deltas) pairs with the execution result and
  the tool card settles. Truncation-shaped misses (``finish_reason`` in
  ``length`` / ``max_tokens``) are left untouched — that is
  MaxTokensBoostMiddleware's territory (a boosted re-call, not a quote fix).
* **Resend** — when repair fails and the message has NO valid call left (the
  only shape where the turn would end), the model is re-called with a
  corrective note appended to the REQUEST only — never to state, never to
  persistence — bounded by ``INVALID_TOOL_CALL["resend_max_retries"]``. The
  streaming callbacks are stripped for the retry exactly like MaxTokensBoost
  does, so the recovered tokens are not streamed to the client a second time.

Repaired calls still pass through the full ``wrap_tool_call`` chain
(ToolGuardrails / PathGuard / HITL approval / sandbox writable-roots) — this
middleware repairs the *syntax* of a call, it bypasses no gate.
"""

from __future__ import annotations

from typing import Any

from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import AIMessage, HumanMessage
from loguru import logger

from agent.trajectory_events import record_trajectory_event
from config.features import INVALID_TOOL_CALL
from runtime.session.state_keys import StateKey

from .repair import repair_tool_args

__all__ = ["InvalidToolCallRepairMiddleware"]

_ENABLED = INVALID_TOOL_CALL["enabled"]
_LOG_ARGS_CHARS = INVALID_TOOL_CALL["log_args_chars"]
_RESEND_RETRIES = INVALID_TOOL_CALL["resend_max_retries"]
_TRUNCATION_REASONS = frozenset({"length", "max_tokens"})
_STREAM_FLAG = StateKey.IS_STREAM_TURN

_RESEND_NOTE = (
    "[Invalid tool call] Your previous tool call did not reach the tools because "
    "its arguments were not valid JSON ({detail}). Re-issue the call now with "
    "correctly escaped JSON arguments: every string value must be wrapped in "
    'double quotes and any inner quote must be escaped as \\". If no tool is '
    "needed, answer in plain text instead."
)


class InvalidToolCallRepairMiddleware(AgentMiddleware):
    """Repair (or re-ask for) tool calls whose JSON arguments failed to parse."""

    # ----- shape helpers ---------------------------------------------------

    @staticmethod
    def _extract_ai_message(result: Any) -> AIMessage | None:
        """Pull the produced ``AIMessage`` out of any result shape we can see.

        Mirrors ``OutputRepetitionGuard._extract_ai_message``: a bare message, a
        ``ModelResponse``, or a nested ``ExtendedModelResponse``.
        """
        if isinstance(result, AIMessage):
            return result
        for attr in ("result", "messages"):
            items = getattr(result, attr, None)
            if isinstance(items, (list, tuple)):
                for msg in items:
                    if isinstance(msg, AIMessage):
                        return msg
        nested = getattr(result, "model_response", None)
        if nested is not None:
            return InvalidToolCallRepairMiddleware._extract_ai_message(nested)
        return None

    @staticmethod
    def _replace_ai_message(result: Any, old: AIMessage, new: AIMessage) -> Any:
        """Swap ``old`` for ``new`` inside the handler result (in place when possible)."""
        if result is old or isinstance(result, AIMessage):
            return new
        for attr in ("result", "messages"):
            items = getattr(result, attr, None)
            if isinstance(items, list):
                for i, item in enumerate(items):
                    if item is old:
                        items[i] = new
                        return result
        # Unknown envelope: return the repaired message itself (the
        # OutputRepetitionGuard hijack precedent this stack already accepts).
        return new

    @staticmethod
    def _finish_reason(ai: AIMessage) -> str | None:
        meta = ai.response_metadata or {}
        return meta.get("finish_reason") or meta.get("stop_reason")

    def _session_id(self, request: Any) -> str:
        state = getattr(request, "state", None) or {}
        session_id = state.get("session_id", "") if hasattr(state, "get") else ""
        return session_id if isinstance(session_id, str) else ""

    def _is_stream_turn(self, request: Any) -> bool:
        """Mirror the ``is_stream_turn`` flag the stream layer writes."""
        from runtime import state_register_mem

        session_id = self._session_id(request)
        if not session_id:
            return False
        return bool(state_register_mem.get_state(session_id, _STREAM_FLAG, ""))

    @staticmethod
    def _strip_callbacks(request: Any) -> Any:
        config = getattr(request, "config", None)
        if isinstance(config, dict):
            return config.pop("callbacks", None)
        return None

    @staticmethod
    def _restore_callbacks(request: Any, original: Any) -> None:
        config = getattr(request, "config", None)
        if isinstance(config, dict):
            config["callbacks"] = original

    # ----- stage 1: repair -------------------------------------------------

    def _record(
        self, session_id: str, call: dict[str, Any], *, repaired: bool, error: str | None
    ) -> None:
        if not session_id:
            return
        raw = call.get("args")
        record_trajectory_event(
            session_id,
            "invalid_tool_call",
            {
                "tool_name": call.get("name"),
                "tool_id": call.get("id"),
                "repaired": repaired,
                "error": error,
                "args_preview": raw[:_LOG_ARGS_CHARS] if isinstance(raw, str) else "",
            },
        )

    def _repair_message(self, ai: AIMessage, session_id: str) -> AIMessage | None:
        """Rebuild the message with every repairable invalid call reattached.

        Returns ``None`` when nothing changes (no invalid calls, a truncation
        shape, or nothing repairable) — the caller then keeps the original.
        """
        invalid = [
            c for c in (getattr(ai, "invalid_tool_calls", None) or []) if isinstance(c, dict)
        ]
        if not invalid:
            return None
        if self._finish_reason(ai) in _TRUNCATION_REASONS:
            # Truncated args are MaxTokensBoost's territory: a quote wrap would
            # "repair" a payload the model never finished writing.
            return None

        recovered: list[dict[str, Any]] = []
        kept: list[dict[str, Any]] = []
        for call in invalid:
            name = call.get("name")
            call_id = call.get("id")
            raw = call.get("args")
            parsed = repair_tool_args(raw) if isinstance(raw, str) else None
            if (
                parsed is not None
                and isinstance(name, str)
                and name
                and isinstance(call_id, str)
                and call_id
            ):
                recovered.append({"name": name, "args": parsed, "id": call_id, "type": "tool_call"})
                logger.info(
                    "[InvalidToolCallRepair] session={} repaired tool call {} ({}) "
                    "after a JSON parse failure",
                    session_id,
                    call_id,
                    name,
                )
                self._record(session_id, call, repaired=True, error=None)
                continue
            kept.append(call)
            error = call.get("error") if isinstance(call.get("error"), str) else None
            logger.warning(
                "[InvalidToolCallRepair] session={} dropping unrepairable tool call "
                "name={} id={}: args preview {!r}",
                session_id,
                name,
                call_id,
                (raw if isinstance(raw, str) else "")[:200],
            )
            self._record(session_id, call, repaired=False, error=error or "args_not_repairable")

        if not recovered:
            return None
        merged = [*(getattr(ai, "tool_calls", None) or []), *recovered]
        return ai.model_copy(update={"tool_calls": merged, "invalid_tool_calls": kept})

    # ----- stage 2: bounded resend ----------------------------------------

    def _build_note(self, ai: AIMessage) -> HumanMessage:
        detail = "the arguments string failed to parse as JSON"
        for call in getattr(ai, "invalid_tool_calls", None) or []:
            error = call.get("error") if isinstance(call, dict) else None
            if isinstance(error, str) and error:
                detail = error
                break
        return HumanMessage(content=_RESEND_NOTE.format(detail=detail))

    def _needs_resend(self, ai: AIMessage) -> bool:
        return not (getattr(ai, "tool_calls", None)) and bool(
            getattr(ai, "invalid_tool_calls", None)
        )

    # ----- hooks -----------------------------------------------------------

    def wrap_model_call(self, request: Any, handler: Any) -> Any:
        """Sync twin: repair, then resend without the stream-callback dance."""
        if not _ENABLED:
            return handler(request)
        result = handler(request)
        ai = self._extract_ai_message(result)
        if ai is None:
            return result
        repaired = self._repair_message(ai, self._session_id(request))
        if repaired is not None:
            result = self._replace_ai_message(result, ai, repaired)
            ai = repaired
        if not self._needs_resend(ai):
            return result
        for attempt in range(1, _RESEND_RETRIES + 1):
            retry_request = request.override(messages=[*request.messages, self._build_note(ai)])
            retry_result = handler(retry_request)
            retry_ai = self._extract_ai_message(retry_result)
            if retry_ai is None:
                continue
            if getattr(retry_ai, "tool_calls", None) or not getattr(
                retry_ai, "invalid_tool_calls", None
            ):
                logger.info(
                    "[InvalidToolCallRepair] session={} recovered via resend attempt {}",
                    self._session_id(request),
                    attempt,
                )
                return retry_result
        logger.warning(
            "[InvalidToolCallRepair] session={} resend budget ({}) exhausted; "
            "returning the invalid-call message (the turn ends as before)",
            self._session_id(request),
            _RESEND_RETRIES,
        )
        return result

    async def awrap_model_call(self, request: Any, handler: Any) -> Any:
        """Async production path: repair, then a bounded, non-duplicating resend."""
        logger.bind(middleware=type(self).__name__).debug("awrap_model_call hook fired")
        if not _ENABLED:
            return await handler(request)
        result = await handler(request)
        ai = self._extract_ai_message(result)
        if ai is None:
            return result
        repaired = self._repair_message(ai, self._session_id(request))
        if repaired is not None:
            result = self._replace_ai_message(result, ai, repaired)
            ai = repaired
        if not self._needs_resend(ai):
            return result

        session_id = self._session_id(request)
        saved_callbacks: Any = None
        stripped = False
        if self._is_stream_turn(request):
            saved_callbacks = self._strip_callbacks(request)
            stripped = True
        try:
            for attempt in range(1, _RESEND_RETRIES + 1):
                retry_request = request.override(messages=[*request.messages, self._build_note(ai)])
                retry_result = await handler(retry_request)
                retry_ai = self._extract_ai_message(retry_result)
                if retry_ai is None:
                    continue
                if getattr(retry_ai, "tool_calls", None) or not getattr(
                    retry_ai, "invalid_tool_calls", None
                ):
                    logger.info(
                        "[InvalidToolCallRepair] session={} recovered via resend attempt {}",
                        session_id,
                        attempt,
                    )
                    return retry_result
            logger.warning(
                "[InvalidToolCallRepair] session={} resend budget ({}) exhausted; "
                "returning the invalid-call message (the turn ends as before)",
                session_id,
                _RESEND_RETRIES,
            )
        finally:
            if stripped:
                self._restore_callbacks(request, saved_callbacks)
        return result
