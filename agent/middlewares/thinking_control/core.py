"""ThinkingControlMiddleware — per-session model and thinking/reasoning control.

The client's chat toolbar writes two per-session values into the state
register:

* ``StateKey.LLM_THINKING_ENABLED`` (``PUT /sessions/thinking``) — the
  thinking choice: a boolean switch, or ``"low"``/``"high"``/``"max"`` for
  always-think models whose control is a selector (glm-5 series).
* ``StateKey.LLM_MAIN_MODEL`` (``PUT /sessions/model``) — a profile descriptor
  ``{id, label, provider, model, base_url, api_key}`` picked from the client's
  env-config profiles, so THIS session runs on that model instead of the
  env-configured main LLM. Absent = follow the env config.

This middleware reads both on every main-chain model call and swaps
``request.model`` for the matching client:

* either control set → a variant built on the calling loop;
* both set → the profile's provider/model with the session's thinking variant
  (the two compose: ``build_main_llm_for_profile(..., thinking=...)``).

Design notes
    * Variants are built lazily on first use and cached per
      ``thinking-key + profile fingerprint`` — one HTTP client per
      (choice) pair, constructed on the calling loop (the loop-binding pitfall
      documented on ``build_main_llm``). This middleware instance is created
      once per compiled graph, and the graph itself is loop-keyed, so the cache
      can never straddle loops. The fingerprint (provider/model/base/key) is
      part of the key so editing a profile in the env config cannot serve a
      stale client.
    * Both flags are read from ``state_register_mem`` only — never the SQLite
      register — so no blocking I/O lands on the event loop. The HTTP layer
      writes both registers and rehydrates mem from db on read, which is what
      makes the choices survive a restart (the client re-GETs on mount).
    * Timing: the swap happens at model-call time, and the HTTP layer refuses
      writes while a turn is in flight — so a switch lands on the NEXT turn,
      never in the middle of one.
    * Disable ladder: gateways whose server default is thinking ON need an
      explicit disable payload; ALWAYS-THINK models (glm-5 series) reject it
      with a "不支持关闭思考" 400. On that rejection the off request is retried
      once with the minimum thinking level ("low") and the learned capability
      is cached, so later off calls skip the failed rung. The retry keeps the
      session's model override.
    * Fail-open: any variant build failure logs a warning and returns the
      original request; the turn proceeds with the env-default model.
"""

import hashlib
from typing import Any

from langchain.agents.middleware import AgentMiddleware
from loguru import logger

from models.LLMs.reasoning_payload import is_thinking_disable_rejection
from runtime.session.state_keys import StateKey


class ThinkingControlMiddleware(AgentMiddleware):
    """Swap the per-call model for the session's model/thinking choice."""

    def __init__(self, temperature: float | None = None) -> None:
        self._temperature = temperature
        self._variants: dict[str, Any] = {}
        # Last applied choice per session, for change-only logging.
        self._last_applied: dict[str, tuple[Any, Any]] = {}
        # Learned capability: the gateway rejects the disable payload.
        self._off_floor_learned = False

    @staticmethod
    def _thinking_kwargs(cache_key: str | None) -> dict[str, Any]:
        """``build_main_llm*`` keyword arguments for one variant key.

        An empty dict means "keep the env default" (``MAIN_LLM_ENABLE_THINKING``)
        — only reachable together with a model override, since a bare thinking
        toggle always resolves to one of the explicit keys.
        """
        if cache_key == "on":
            return {"thinking": True}
        if cache_key == "off_floor":
            return {"thinking": False, "thinking_floor": True}
        if cache_key == "off":
            return {"thinking": False}
        if cache_key and cache_key.startswith("level:"):
            return {"thinking_level": cache_key.split(":", 1)[1]}
        return {}

    def _build(self, cache_key: str | None, override: dict | None) -> Any:
        from models import build_main_llm

        thinking_kwargs = self._thinking_kwargs(cache_key)
        if override is None:
            return build_main_llm(temperature=self._temperature, **thinking_kwargs)

        from models import build_main_llm_for_profile

        return build_main_llm_for_profile(
            provider=override.get("provider"),
            model=override["model"],
            api_key=override.get("api_key"),
            base_url=override.get("base_url"),
            temperature=self._temperature,
            **thinking_kwargs,
        )

    @staticmethod
    def _override_signature(override: dict | None) -> str:
        """Cache-key fragment identifying the exact profile configuration.

        Provider/model/base/key all participate: editing a profile (new key or
        gateway) must not serve the previously cached client, while two
        sessions choosing the same profile share one.
        """
        if not override:
            return "env"
        fields = "|".join(
            str(override.get(field) or "") for field in ("provider", "model", "base_url", "api_key")
        )
        digest = hashlib.blake2s(fields.encode("utf-8"), usedforsecurity=False).hexdigest()[:12]
        return f"{override.get('model')}#{digest}"

    def _variant(self, cache_key: str | None, override: dict | None) -> Any:
        signature = f"{cache_key or 'default'}::{self._override_signature(override)}"
        cached = self._variants.get(signature)
        if cached is None:
            cached = self._build(cache_key, override)
            self._variants[signature] = cached
        return cached

    def _cache_key_for(self, value: Any) -> str | None:
        """Map a stored thinking flag to a variant cache key (None = no swap)."""
        if isinstance(value, bool):
            if value:
                return "on"
            # After the gateway taught us it always thinks, go straight to the
            # minimum-thinking variant instead of replaying the rejected rung.
            return "off_floor" if self._off_floor_learned else "off"
        if isinstance(value, str) and value in ("low", "high", "max"):
            return f"level:{value}"
        return None

    def _read_controls(self, session_id: str) -> tuple[str | None, dict | None]:
        """(thinking cache key, model override) as stored for the session.

        Mem-register only (no blocking I/O on the loop). A malformed override
        (no usable model name) is ignored, mirroring the service's validation.
        """
        from runtime import state_register_mem

        desired = state_register_mem.get_state(session_id, StateKey.LLM_THINKING_ENABLED, None)
        cache_key = self._cache_key_for(desired)
        raw_override = state_register_mem.get_state(session_id, StateKey.LLM_MAIN_MODEL, None)
        override = (
            raw_override if isinstance(raw_override, dict) and raw_override.get("model") else None
        )
        if cache_key is None and override is None:
            return None, None
        applied = (cache_key, (override or {}).get("model"))
        if self._last_applied.get(session_id) != applied:
            logger.info(
                "Model control: session {} → model={} thinking={} (model call override)",
                session_id,
                (override or {}).get("model") or "env",
                cache_key or "env-default",
            )
            self._last_applied[session_id] = applied
        return cache_key, override

    def _maybe_swap(self, request: Any) -> tuple[Any, str | None, dict | None]:
        """Return (request, applied_thinking_key, override) — original when no-op."""
        state = getattr(request, "state", None) or {}
        session_id = state.get("session_id") if hasattr(state, "get") else None
        if not session_id:
            return request, None, None

        cache_key, override = self._read_controls(session_id)
        if cache_key is None and override is None:
            return request, None, None

        try:
            variant = self._variant(cache_key, override)
        except Exception as exc:
            logger.warning(
                "Model variant build failed (thinking={}, model={}), keeping env-default model: {}",
                cache_key,
                (override or {}).get("model"),
                exc,
            )
            return request, None, None
        return request.override(model=variant), cache_key, override

    def _retry_with_floor(self, request: Any, handler: Any, override: dict | None) -> Any:
        """Re-run the handler with the minimum-thinking variant after the
        gateway rejected the disable payload (always-think model)."""
        self._off_floor_learned = True
        logger.info("Gateway rejects thinking-off; falling back to the minimum thinking level")
        try:
            floor_variant = self._variant("off_floor", override)
        except Exception as exc:
            logger.warning("Thinking floor variant build failed: {}", exc)
            raise
        swapped = request.override(model=floor_variant)
        return handler(swapped)

    def wrap_model_call(self, request: Any, handler: Any) -> Any:
        swapped, cache_key, override = self._maybe_swap(request)
        if cache_key != "off" or self._off_floor_learned:
            return handler(swapped)
        try:
            return handler(swapped)
        except Exception as exc:
            if is_thinking_disable_rejection(exc):
                return self._retry_with_floor(request, handler, override)
            raise

    async def awrap_model_call(self, request: Any, handler: Any) -> Any:
        swapped, cache_key, override = self._maybe_swap(request)
        if cache_key != "off" or self._off_floor_learned:
            return await handler(swapped)
        try:
            return await handler(swapped)
        except Exception as exc:
            if is_thinking_disable_rejection(exc):
                return self._retry_with_floor(request, handler, override)
            raise
