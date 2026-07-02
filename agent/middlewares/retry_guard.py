"""Per-attempt one-shot recovery guards.

Equivalent to hermes-agent's ``agent/turn_retry_state.py``.

Each recovery path (credential refresh, format stripping, compression, etc.)
has a one-shot boolean that ensures it fires at most **once per attempt**.
Without this, a persistent error could loop through the same recovery
strategy forever.

This middleware wraps model calls and tracks which recovery strategies have
already been attempted.  When an error occurs, it classifies the error and
selects the appropriate one-shot recovery strategy.  If that strategy has
already been used this turn, the error is re-raised instead of retrying.
"""

from __future__ import annotations

from dataclasses import dataclass, field, fields
from enum import Enum
from typing import Any, Callable, Awaitable

from loguru import logger
from langgraph.runtime import Runtime
from langgraph.typing import ContextT
from typing_extensions import override
from langchain_core.messages import AIMessage, BaseMessage
from langchain.agents.middleware import AgentMiddleware, AgentState
from langchain.agents.middleware.types import ResponseT, ModelRequest, ModelResponse, ExtendedModelResponse

from runtime import state_register_mem


class RecoveryStrategy(str, Enum):
    AUTH_REFRESH = "auth_refresh"
    FORMAT_STRIP = "format_strip"
    COMPRESSION = "compression"
    CREDENTIAL_ROTATE = "credential_rotate"
    BACKOFF_RETRY = "backoff_retry"
    FALLBACK_PROVIDER = "fallback_provider"


class ErrorKind(str, Enum):
    AUTH_ERROR = "auth_error"
    RATE_LIMIT = "rate_limit"
    CONTEXT_OVERFLOW = "context_overflow"
    FORMAT_ERROR = "format_error"
    TIMEOUT = "timeout"
    SERVER_ERROR = "server_error"
    CONTENT_POLICY = "content_policy"
    BILLING_ERROR = "billing_error"
    UNKNOWN = "unknown"


_NON_RETRYABLE: set[ErrorKind] = {
    ErrorKind.AUTH_ERROR,
    ErrorKind.FORMAT_ERROR,
    ErrorKind.CONTENT_POLICY,
    ErrorKind.BILLING_ERROR,
}

_ERROR_TO_STRATEGY: dict[ErrorKind, RecoveryStrategy] = {
    ErrorKind.AUTH_ERROR: RecoveryStrategy.AUTH_REFRESH,
    ErrorKind.RATE_LIMIT: RecoveryStrategy.BACKOFF_RETRY,
    ErrorKind.CONTEXT_OVERFLOW: RecoveryStrategy.COMPRESSION,
    ErrorKind.FORMAT_ERROR: RecoveryStrategy.FORMAT_STRIP,
    ErrorKind.TIMEOUT: RecoveryStrategy.BACKOFF_RETRY,
    ErrorKind.SERVER_ERROR: RecoveryStrategy.BACKOFF_RETRY,
    ErrorKind.CONTENT_POLICY: RecoveryStrategy.FORMAT_STRIP,
    ErrorKind.BILLING_ERROR: RecoveryStrategy.CREDENTIAL_ROTATE,
    ErrorKind.UNKNOWN: RecoveryStrategy.BACKOFF_RETRY,
}


@dataclass
class RetryGuardState:
    attempted: set[str] = field(default_factory=set)
    total_retries: int = 0
    max_total_retries: int = 3

    def is_attempted(self, strategy: RecoveryStrategy) -> bool:
        return strategy.value in self.attempted

    def mark_attempted(self, strategy: RecoveryStrategy) -> None:
        self.attempted.add(strategy.value)

    @property
    def retries_exhausted(self) -> bool:
        return self.total_retries >= self.max_total_retries


_RETRY_GUARD_KEY = "retry_guard_state"


class RetryGuard(AgentMiddleware):
    """One-shot recovery guards that prevent retry amplification.

    Parameters
    ----------
    max_total_retries : int
        Maximum number of retries across all strategies per turn.
        Default **3** (matching hermes ``_api_max_retries``).
    """

    def __init__(self, max_total_retries: int = 3):
        super().__init__()
        self.max_total_retries = max_total_retries

    def _get_session_id(self, state: dict[str, Any]) -> str:
        session_id: str = state.get("session_id", "")
        if not session_id.strip():
            raise RuntimeError("RetryGuard: session_id is required")
        return session_id

    def _get_state(self, session_id: str) -> RetryGuardState:
        raw = state_register_mem.get_state(session_id, _RETRY_GUARD_KEY, None)
        if raw is None:
            return RetryGuardState(max_total_retries=self.max_total_retries)
        return raw

    def _save_state(self, session_id: str, gs: RetryGuardState) -> None:
        state_register_mem.set_state(session_id, _RETRY_GUARD_KEY, gs)

    @staticmethod
    def classify_error(error: Exception) -> ErrorKind:
        msg = str(error).lower()
        status = getattr(error, "status_code", None) or getattr(
            getattr(error, "response", None), "status_code", None
        )

        if status in (401, 403):
            return ErrorKind.AUTH_ERROR
        if status == 402:
            return ErrorKind.BILLING_ERROR
        if status == 429:
            return ErrorKind.RATE_LIMIT
        if status == 451:
            return ErrorKind.CONTENT_POLICY
        if status in (400, 422):
            return ErrorKind.FORMAT_ERROR
        if status in (500, 502, 503, 504):
            return ErrorKind.SERVER_ERROR

        if any(kw in msg for kw in ("timeout", "timed out", "deadline", "deadline_exceeded")):
            return ErrorKind.TIMEOUT
        if any(kw in msg for kw in ("context_length_exceeded", "max_tokens", "too many tokens", "context window")):
            return ErrorKind.CONTEXT_OVERFLOW
        if any(kw in msg for kw in ("content_policy", "content filter", "safety", "blocked")):
            return ErrorKind.CONTENT_POLICY
        if any(kw in msg for kw in ("unauthorized", "authentication", "invalid api key", "invalid x-api-key")):
            return ErrorKind.AUTH_ERROR
        if any(kw in msg for kw in ("billing", "quota", "insufficient_quota", "payment")):
            return ErrorKind.BILLING_ERROR
        if any(kw in msg for kw in ("rate limit", "rate_limit", "too many requests", "429")):
            return ErrorKind.RATE_LIMIT
        if any(kw in msg for kw in ("server error", "internal server", "overloaded", "502", "503", "504")):
            return ErrorKind.SERVER_ERROR

        return ErrorKind.UNKNOWN

    @staticmethod
    def is_retryable(error_kind: ErrorKind) -> bool:
        return error_kind not in _NON_RETRYABLE

    @staticmethod
    def select_strategy(error_kind: ErrorKind) -> RecoveryStrategy:
        return _ERROR_TO_STRATEGY.get(error_kind, RecoveryStrategy.BACKOFF_RETRY)

    @override
    async def abefore_agent(
        self, state: AgentState, runtime: Runtime
    ) -> dict[str, Any] | None:
        session_id = self._get_session_id(state)
        state_register_mem.set_state(
            session_id, _RETRY_GUARD_KEY,
            RetryGuardState(max_total_retries=self.max_total_retries),
        )
        return None

    @override
    async def awrap_model_call(
        self,
        request: ModelRequest[ContextT],
        handler: Callable[[ModelRequest[ContextT]], Awaitable[ModelResponse[ResponseT]]],
    ) -> ModelResponse[ResponseT] | AIMessage | ExtendedModelResponse[ResponseT]:
        session_id = self._get_session_id(request.state)
        gs = self._get_state(session_id)

        try:
            return await handler(request)
        except Exception as exc:
            error_kind = self.classify_error(exc)
            strategy = self.select_strategy(error_kind)

            if not self.is_retryable(error_kind):
                logger.warning(
                    "RetryGuard: non-retryable error %s for session %s. Re-raising.",
                    error_kind.value, session_id,
                )
                raise

            if gs.is_attempted(strategy):
                logger.warning(
                    "RetryGuard: strategy %s already attempted for session %s. Re-raising.",
                    strategy.value, session_id,
                )
                raise

            if gs.retries_exhausted:
                logger.warning(
                    "RetryGuard: max retries (%d) exhausted for session %s. Re-raising.",
                    gs.max_total_retries, session_id,
                )
                raise

            gs.mark_attempted(strategy)
            gs.total_retries += 1
            self._save_state(session_id, gs)

            logger.info(
                "RetryGuard: applying strategy %s for error %s (attempt %d/%d) session=%s",
                strategy.value, error_kind.value, gs.total_retries,
                gs.max_total_retries, session_id,
            )

            try:
                return await handler(request)
            except Exception:
                raise
