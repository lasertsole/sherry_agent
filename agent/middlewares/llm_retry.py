"""LLMRetryMiddleware — generic error retry loop around each model call.

Relationship to the existing pipeline:
- Wraps the Summarization middleware's T4/T5 overflow-recovery loop from the
  outside; Summarization keeps owning payload_too_large / context_overflow
  (``should_compress`` errors re-raise immediately into it).
- Handles the remaining classes: timeout, rate_limit, overloaded,
  server_error, invalid_response, unknown (bounded backoff retries) and
  delegates deterministic failures (auth_permanent, billing, ssl, model
  not found, policy blocks) to the fallback chain when one is configured.
- content_policy_blocked never retries: the stream layer flags it via the
  ``llm_content_filter_blocked`` state key and this middleware consumes it
  after each handler call, switching to a fallback model or raising
  :class:`ContentFilterError`.
- The stale-streak circuit breaker counts timeout-classified failures across
  turns (session-scoped ``llm_stale_streak``) and aborts the turn once the
  provider looks persistently unresponsive.

The async ``awrap_model_call`` is the production path (the main agent runs
via astream/ainvoke); the sync twin exists for parity with the other
middlewares. When no fallback chain is configured (the default) the
middleware is a plain bounded retry loop.
"""

import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from langchain.agents.middleware import AgentMiddleware
from langchain.agents.middleware.types import ModelRequest
from loguru import logger

from pub_func.message.llm_error_classifier import FailoverReason, classify_api_error
from pub_func.retry_utils import jittered_backoff
from runtime import state_register_mem

_STALE_STREAK_KEY = "llm_stale_streak"
_FALLBACK_INDEX_KEY = "llm_fallback_index"
_CONTENT_FILTER_KEY = "llm_content_filter_blocked"
_STALE_GIVEUP_MESSAGE = "Provider unresponsive — aborting to avoid indefinite stall."
_CONTENT_FILTER_MESSAGE = "Model declined to respond (safety refusal)."


class ContentFilterError(Exception):
    """The model (or the stream layer) flagged the response as safety-filtered."""


@dataclass
class LLMRetryConfig:
    max_retries: int = 3
    base_delay: float = 2.0
    max_delay: float = 60.0
    jitter: float = 0.3
    stale_giveup_threshold: int = 5


@dataclass
class FallbackCandidate:
    provider: str
    model_name: str
    model: Any


class LLMRetryMiddleware(AgentMiddleware):
    """Bounded retry / fallback loop composed around every model call."""

    def __init__(
        self,
        config: LLMRetryConfig | None = None,
        fallback_chain: list[FallbackCandidate] | None = None,
    ):
        self.config = config or LLMRetryConfig()
        self.fallback_chain = fallback_chain or []

    # ---- hooks ----------------------------------------------------------

    def wrap_model_call(self, request: ModelRequest, handler: Callable) -> Any:
        session_id = self._get_session_id(request)
        if session_id is None:
            return handler(request)
        request = self._apply_sticky_fallback(request, session_id)
        retry_count = 0
        while True:
            self._raise_if_provider_stale(session_id)
            try:
                result = handler(request)
            except Exception as exc:
                classified = classify_api_error(exc)
                if classified.should_compress:
                    raise  # Summarization's T4/T5 recovery loop owns overflow errors
                rebound = self._handle_content_filter_flag(request, session_id, cause=exc)
                if rebound is not None:
                    request = rebound
                    retry_count = 0
                    continue
                if not classified.retryable:
                    rebound = None
                    if classified.should_fallback:
                        rebound = self._try_fallback(request, session_id)
                    if rebound is not None:
                        request = rebound
                        retry_count = 0
                        continue
                    raise
                retry_count += 1
                if retry_count > self.config.max_retries:
                    raise
                if classified.reason == FailoverReason.timeout:
                    self._bump_stale_streak(session_id)
                delay = self._backoff(retry_count)
                logger.warning(
                    "LLM call failed (attempt {}/{}, reason={}): retrying in {:.2f}s — {}",
                    retry_count,
                    self.config.max_retries,
                    classified.reason.value,
                    delay,
                    exc,
                )
                time.sleep(delay)
                continue
            self._reset_stale_streak(session_id)
            rebound = self._handle_content_filter_flag(request, session_id)
            if rebound is not None:
                request = rebound
                retry_count = 0
                continue
            return result

    async def awrap_model_call(
        self, request: ModelRequest, handler: Callable[[ModelRequest], Awaitable[Any]]
    ) -> Any:
        session_id = self._get_session_id(request)
        if session_id is None:
            return await handler(request)
        request = self._apply_sticky_fallback(request, session_id)
        retry_count = 0
        while True:
            self._raise_if_provider_stale(session_id)
            try:
                result = await handler(request)
            except Exception as exc:
                classified = classify_api_error(exc)
                if classified.should_compress:
                    raise  # Summarization's T4/T5 recovery loop owns overflow errors
                rebound = self._handle_content_filter_flag(request, session_id, cause=exc)
                if rebound is not None:
                    request = rebound
                    retry_count = 0
                    continue
                if not classified.retryable:
                    rebound = None
                    if classified.should_fallback:
                        rebound = self._try_fallback(request, session_id)
                    if rebound is not None:
                        request = rebound
                        retry_count = 0
                        continue
                    raise
                retry_count += 1
                if retry_count > self.config.max_retries:
                    raise
                if classified.reason == FailoverReason.timeout:
                    self._bump_stale_streak(session_id)
                delay = self._backoff(retry_count)
                logger.warning(
                    "LLM call failed (attempt {}/{}, reason={}): retrying in {:.2f}s — {}",
                    retry_count,
                    self.config.max_retries,
                    classified.reason.value,
                    delay,
                    exc,
                )
                await asyncio.sleep(delay)
                continue
            self._reset_stale_streak(session_id)
            rebound = self._handle_content_filter_flag(request, session_id)
            if rebound is not None:
                request = rebound
                retry_count = 0
                continue
            return result

    # ---- session-scoped state -------------------------------------------

    def _get_session_id(self, request: ModelRequest) -> str | None:
        state = getattr(request, "state", None) or {}
        session_id = state.get("session_id") if hasattr(state, "get") else None
        return session_id or None

    def _raise_if_provider_stale(self, session_id: str) -> None:
        streak = state_register_mem.get_state(session_id, _STALE_STREAK_KEY, 0) or 0
        if streak >= self.config.stale_giveup_threshold:
            logger.error(
                "Stale-streak circuit breaker tripped (streak={}): {}",
                streak,
                _STALE_GIVEUP_MESSAGE,
            )
            raise RuntimeError(_STALE_GIVEUP_MESSAGE)

    def _bump_stale_streak(self, session_id: str) -> None:
        streak = state_register_mem.get_state(session_id, _STALE_STREAK_KEY, 0) or 0
        state_register_mem.set_state(session_id, _STALE_STREAK_KEY, streak + 1)

    def _reset_stale_streak(self, session_id: str) -> None:
        state_register_mem.set_state(session_id, _STALE_STREAK_KEY, 0)

    def _backoff(self, retry_count: int) -> float:
        return jittered_backoff(
            retry_count,
            base_delay=self.config.base_delay,
            max_delay=self.config.max_delay,
            jitter=self.config.jitter,
        )

    # ---- content-filter flag (set by the stream layer) -------------------

    def _handle_content_filter_flag(
        self, request: ModelRequest, session_id: str, cause: BaseException | None = None
    ) -> ModelRequest | None:
        """Consume the cross-layer content-filter flag after a handler call.

        Returns the request rebound to the fallback model when a candidate is
        available, or None when the flag is absent / no candidate remains
        (raising :class:`ContentFilterError` when the flag WAS set). This is
        also the interception seam for stream-layer response markers.
        """
        if not state_register_mem.get_state(session_id, _CONTENT_FILTER_KEY, False):
            return None
        state_register_mem.set_state(session_id, _CONTENT_FILTER_KEY, False)
        rebound = self._try_fallback(request, session_id)
        if rebound is not None:
            logger.warning(
                "Content filter flagged the model response — switching to fallback model"
            )
            return rebound
        raise ContentFilterError(_CONTENT_FILTER_MESSAGE) from cause

    # ---- fallback chain ---------------------------------------------------

    def _apply_sticky_fallback(self, request: ModelRequest, session_id: str) -> ModelRequest:
        """Begin attempts on the already-activated fallback candidate, if any."""
        if not self.fallback_chain:
            return request
        idx = state_register_mem.get_state(session_id, _FALLBACK_INDEX_KEY, 0) or 0
        if not 0 < idx <= len(self.fallback_chain):
            return request
        return self._rebind_model(request, self.fallback_chain[idx - 1]) or request

    def _try_fallback(self, request: ModelRequest, session_id: str) -> ModelRequest | None:
        """Activate the next fallback candidate; None when the chain is exhausted."""
        if not self.fallback_chain:
            return None
        idx = state_register_mem.get_state(session_id, _FALLBACK_INDEX_KEY, 0) or 0
        if idx >= len(self.fallback_chain):
            return None
        candidate = self.fallback_chain[idx]
        state_register_mem.set_state(session_id, _FALLBACK_INDEX_KEY, idx + 1)
        rebound = self._rebind_model(request, candidate)
        if rebound is None:
            return None
        logger.warning(
            "Switching to fallback model {}/{} (candidate {} of {})",
            candidate.provider,
            candidate.model_name,
            idx + 1,
            len(self.fallback_chain),
        )
        return rebound

    def _rebind_model(
        self, request: ModelRequest, candidate: FallbackCandidate
    ) -> ModelRequest | None:
        try:
            return request.override(model=candidate.model)
        except Exception as exc:
            logger.error(
                "Failed to rebind request to fallback model {}/{}: {}",
                candidate.provider,
                candidate.model_name,
                exc,
            )
            return None
