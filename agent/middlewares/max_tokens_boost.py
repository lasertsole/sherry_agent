"""MaxTokensBoostMiddleware — tool-call truncation recovery (token-limit plan Phase 3).

When a model call comes back truncated (``finish_reason == "length"`` /
``stop_reason == "max_tokens"``) AND the response carries tool calls, the
tool-call JSON itself was cut off — executing it would be garbage. This
middleware re-calls the handler inside ``wrap_model_call`` /
``awrap_model_call`` with a boosted ``max_tokens`` (base × 2^attempt,
capped at 32768) so the model can emit the complete tool-call payload. The
boost base resolves through three layers — the call's own
``model_settings["max_tokens"]``, then the ``MAIN_LLM_OUTPUT_MAX_TOKEN``
env base, then the 8192 default — so a re-call tracks the current call's
actual limit instead of restarting from the env default.

Text-only truncation (no tool calls) is NOT handled here — the StreamTurn
outer loop (Phase 2) owns it via a continuation HumanMessage.

Both streaming and non-streaming paths re-call the handler uniformly. For the
streaming path the LangChain callbacks are stripped before each re-call so the
recovered tokens are not streamed to the client a second time (the truncated
first-call tokens already went out on the wire); the original callbacks are
restored in ``finally``. The agent loop only ever sees the final result, so
truncated intermediate responses never reach the checkpointer and the
IterationBudget is charged once per outer model call.
"""

import logging
import os
from typing import Any

from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import AIMessage

_logger = logging.getLogger(__name__)

_BASE_MAX_TOKENS = int(os.getenv("MAIN_LLM_OUTPUT_MAX_TOKEN", "8192"))
_DEFAULT_MAX_TOKENS = 8192
_MAX_CAP = 32_768
_MAX_RETRIES = 3
_TRUNCATION_REASONS = frozenset({"length", "max_tokens"})
_STREAM_FLAG = "is_stream_turn"


class MaxTokensBoostMiddleware(AgentMiddleware):
    """Re-call the model with a boosted ``max_tokens`` when tool calls get truncated."""

    def _extract_ai_message(self, result: Any) -> AIMessage | None:
        """Pull the AIMessage out of the handler result.

        The handler may return a raw ``AIMessage`` (bare model call) or a
        ``ModelRequest``-shaped response object carrying ``.messages``.
        """
        if isinstance(result, AIMessage):
            return result
        messages = getattr(result, "messages", None)
        if messages:
            for msg in reversed(messages):
                if isinstance(msg, AIMessage):
                    return msg
        return None

    def _detect_tool_call_truncation(self, result: Any) -> bool:
        ai = self._extract_ai_message(result)
        if ai is None:
            return False
        finish = (ai.response_metadata or {}).get("finish_reason") or (
            ai.response_metadata or {}
        ).get("stop_reason")
        return bool(finish in _TRUNCATION_REASONS and getattr(ai, "tool_calls", None))

    def _inject_boost(self, request: Any, max_tokens: int) -> None:
        model_settings = getattr(request, "model_settings", None)
        if isinstance(model_settings, dict):
            model_settings["max_tokens"] = max_tokens

    def _get_current_base(self, request: Any) -> int:
        """Resolve the boost base through three layers, first positive value wins.

        Layer 1: the call's own ``model_settings["max_tokens"]`` (the actual
        current limit — boosts never restart from a lower default);
        Layer 2: the ``MAIN_LLM_OUTPUT_MAX_TOKEN`` env base;
        Layer 3: the hardcoded 8192 default.
        """
        model_settings = getattr(request, "model_settings", None)
        if isinstance(model_settings, dict):
            current = model_settings.get("max_tokens")
            if isinstance(current, int) and current > 0:
                return current
        return _BASE_MAX_TOKENS if _BASE_MAX_TOKENS > 0 else _DEFAULT_MAX_TOKENS

    def _strip_callbacks(self, request: Any) -> Any:
        config = getattr(request, "config", None)
        if isinstance(config, dict):
            return config.pop("callbacks", None)
        return None

    def _restore_callbacks(self, request: Any, original: Any) -> None:
        config = getattr(request, "config", None)
        if isinstance(config, dict):
            config["callbacks"] = original

    def _is_stream_turn(self, request: Any) -> bool:
        """Mirror the ``is_stream_turn`` flag set by StreamTurn.run().

        The flag is keyed by the session id from the agent state (same key the
        dispatch loop writes); no flag / unknown session = treat as non-stream.
        """
        from runtime import state_register_mem

        state = getattr(request, "state", None) or {}
        session_id = state.get("session_id") if hasattr(state, "get") else None
        if not session_id:
            return False
        return bool(state_register_mem.get_state(session_id, _STREAM_FLAG, ""))

    def wrap_model_call(self, request: Any, handler: Any) -> Any:
        result = handler(request)
        if not self._detect_tool_call_truncation(result):
            return result
        base = self._get_current_base(request)
        for _attempt in range(1, _MAX_RETRIES + 1):
            boosted = min(base * (2**_attempt), _MAX_CAP)
            self._inject_boost(request, boosted)
            result = handler(request)
            if not self._detect_tool_call_truncation(result):
                break
        return result

    async def awrap_model_call(self, request: Any, handler: Any) -> Any:
        result = await handler(request)
        if not self._detect_tool_call_truncation(result):
            return result
        saved_callbacks: Any = None
        stripped = False
        if self._is_stream_turn(request):
            saved_callbacks = self._strip_callbacks(request)
            stripped = True
        try:
            base = self._get_current_base(request)
            for attempt in range(1, _MAX_RETRIES + 1):
                boosted = min(base * (2**attempt), _MAX_CAP)
                self._inject_boost(request, boosted)
                result = await handler(request)
                if not self._detect_tool_call_truncation(result):
                    break
            else:
                _logger.warning(
                    "MaxTokensBoost exhausted %s retries (base=%s, cap=%s); "
                    "returning last truncated result",
                    _MAX_RETRIES,
                    base,
                    _MAX_CAP,
                )
        finally:
            if stripped:
                self._restore_callbacks(request, saved_callbacks)
        return result
