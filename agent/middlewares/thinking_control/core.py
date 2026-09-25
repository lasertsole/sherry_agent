"""ThinkingControlMiddleware — per-session model thinking/reasoning control.

The client's thinking control (multimedia toolbar → ``PUT /sessions/thinking``)
persists a per-session flag in the state register. This middleware reads that
flag on every main-chain model call and, when the user made an explicit
choice, swaps ``request.model`` for the matching client variant built by
``build_main_llm``:

* boolean flag → thinking on/off variant (the env default applies when unset);
* ``"low"`` / ``"high"`` / ``"max"`` → an explicit level for always-think
  models whose toggle is a selector rather than a switch (glm-5 series).

Design notes
    * The variant clients are built lazily on first use and cached per value —
      one HTTP client per state, constructed on the calling loop (the
      loop-binding pitfall documented on ``build_main_llm``). This middleware
      instance is created once per compiled graph, and the graph itself is
      loop-keyed, so the cache can never straddle loops.
    * The flag is read from ``state_register_mem`` only — never the SQLite
      register — so no blocking I/O lands on the event loop. The HTTP layer
      writes both registers and rehydrates mem from db on read, which is
      what makes the flag survive a restart (the client re-GETs on mount).
    * Disable ladder: gateways whose server default is thinking ON need an
      explicit disable payload; ALWAYS-THINK models (glm-5 series) reject it
      with a "不支持关闭思考" 400. On that rejection the off request is retried
      once with the minimum thinking level ("low") and the learned capability
      is cached, so later off calls skip the failed rung.
    * Fail-open: any variant build failure logs a warning and returns the
      original request; the turn proceeds with the env-default model.
"""

from typing import Any

from langchain.agents.middleware import AgentMiddleware
from loguru import logger

from models.LLMs.reasoning_payload import is_thinking_disable_rejection
from runtime.session.state_keys import StateKey


class ThinkingControlMiddleware(AgentMiddleware):
    """Swap the per-call model for the session's thinking variant."""

    def __init__(self, temperature: float | None = None) -> None:
        self._temperature = temperature
        self._variants: dict[str, Any] = {}
        # Last applied state per session, for flip-only logging.
        self._last_applied: dict[str, Any] = {}
        # Learned capability: the gateway rejects the disable payload.
        self._off_floor_learned = False

    def _build(self, cache_key: str) -> Any:
        from models import build_main_llm

        if cache_key == "on":
            return build_main_llm(temperature=self._temperature, thinking=True)
        if cache_key == "off_floor":
            return build_main_llm(
                temperature=self._temperature, thinking=False, thinking_floor=True
            )
        if cache_key.startswith("level:"):
            return build_main_llm(
                temperature=self._temperature, thinking_level=cache_key.split(":", 1)[1]
            )
        return build_main_llm(temperature=self._temperature, thinking=False)

    def _variant(self, cache_key: str) -> Any:
        cached = self._variants.get(cache_key)
        if cached is None:
            cached = self._build(cache_key)
            self._variants[cache_key] = cached
        return cached

    def _cache_key_for(self, value: Any) -> str | None:
        """Map a stored flag value to a variant cache key (None = no swap)."""
        if isinstance(value, bool):
            if value:
                return "on"
            # After the gateway taught us it always thinks, go straight to the
            # minimum-thinking variant instead of replaying the rejected rung.
            return "off_floor" if self._off_floor_learned else "off"
        if isinstance(value, str) and value in ("low", "high", "max"):
            return f"level:{value}"
        return None

    def _maybe_swap(self, request: Any) -> tuple[Any, str | None]:
        """Return (request, applied_cache_key) — original request when no-op."""
        state = getattr(request, "state", None) or {}
        session_id = state.get("session_id") if hasattr(state, "get") else None
        if not session_id:
            return request, None

        from runtime import state_register_mem

        desired = state_register_mem.get_state(session_id, StateKey.LLM_THINKING_ENABLED, None)
        cache_key = self._cache_key_for(desired)
        if cache_key is None:
            return request, None

        if self._last_applied.get(session_id) != desired:
            logger.info(
                "Thinking control: session {} → thinking={} (model call override)",
                session_id,
                desired,
            )
            self._last_applied[session_id] = desired

        try:
            variant = self._variant(cache_key)
        except Exception as exc:
            logger.warning(
                "Thinking variant build failed ({}), keeping env-default model: {}",
                cache_key,
                exc,
            )
            return request, None
        return request.override(model=variant), cache_key

    def _retry_with_floor(self, request: Any, handler: Any) -> Any:
        """Re-run the handler with the minimum-thinking variant after the
        gateway rejected the disable payload (always-think model)."""
        self._off_floor_learned = True
        logger.info("Gateway rejects thinking-off; falling back to the minimum thinking level")
        try:
            floor_variant = self._variant("off_floor")
        except Exception as exc:
            logger.warning("Thinking floor variant build failed: {}", exc)
            raise
        return handler(request.override(model=floor_variant))

    def wrap_model_call(self, request: Any, handler: Any) -> Any:
        swapped, cache_key = self._maybe_swap(request)
        if cache_key != "off" or self._off_floor_learned:
            return handler(swapped)
        try:
            return handler(swapped)
        except Exception as exc:
            if is_thinking_disable_rejection(exc):
                return self._retry_with_floor(request, handler)
            raise

    async def awrap_model_call(self, request: Any, handler: Any) -> Any:
        swapped, cache_key = self._maybe_swap(request)
        if cache_key != "off" or self._off_floor_learned:
            return await handler(swapped)
        try:
            return await handler(swapped)
        except Exception as exc:
            if is_thinking_disable_rejection(exc):
                return await self._retry_with_floor(request, handler)
            raise
