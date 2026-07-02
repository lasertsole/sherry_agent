"""Compression anti-thrashing guard.

Equivalent to hermes-agent's compression anti-thrashing mechanisms from
``context_compressor.py`` and ``conversation_loop.py``:

- Max compression attempts (default 3)
- Anti-thrashing: skip compression if last two saved < 10% each
- Output-cap error fast-fail (compression cannot help)
- Compression effectiveness check (if neither message count nor tokens
  decreased by at least 5%, terminate instead of retrying)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Awaitable

from loguru import logger
from langgraph.runtime import Runtime
from langgraph.typing import ContextT
from typing_extensions import override
from langchain_core.messages import AIMessage, BaseMessage
from langchain.agents.middleware import AgentMiddleware, AgentState
from langchain.agents.middleware.types import ResponseT, ModelRequest, ModelResponse, ExtendedModelResponse

from runtime import state_register_mem


@dataclass
class CompressionGuardState:
    compression_attempts: int = 0
    ineffective_count: int = 0
    last_msg_count: int | None = None
    last_token_estimate: int | None = None


_COMPRESSION_GUARD_KEY = "compression_guard_state"


@dataclass
class CompressionGuardConfig:
    max_compression_attempts: int = 3
    ineffective_threshold: int = 2
    min_reduction_pct: float = 0.10
    min_effectiveness_pct: float = 0.05


class CompressionGuard(AgentMiddleware):
    """Prevent compression death-loops.

    Tracks compression attempts and effectiveness.  If compression
    repeatedly fails to reduce context size, the middleware forces the
    model call to proceed anyway (or returns a terminal message if the
    budget is truly exhausted).

    Parameters
    ----------
    config : CompressionGuardConfig | None
        Tuning knobs.
    """

    def __init__(self, config: CompressionGuardConfig | None = None):
        super().__init__()
        self.config = config or CompressionGuardConfig()

    def _get_session_id(self, state: dict[str, Any]) -> str:
        session_id: str = state.get("session_id", "")
        if not session_id.strip():
            raise RuntimeError("CompressionGuard: session_id is required")
        return session_id

    def _get_state(self, session_id: str) -> CompressionGuardState:
        return state_register_mem.get_state(session_id, _COMPRESSION_GUARD_KEY, CompressionGuardState())

    def _save_state(self, session_id: str, gs: CompressionGuardState) -> None:
        state_register_mem.set_state(session_id, _COMPRESSION_GUARD_KEY, gs)

    @staticmethod
    def _estimate_tokens(messages: list[BaseMessage]) -> int:
        total = 0
        for m in messages:
            content = getattr(m, "content", "")
            total += len(str(content)) // 4
        return total

    @staticmethod
    def is_output_cap_error(error: Exception) -> bool:
        msg = str(error).lower()
        return any(kw in msg for kw in ("max_tokens", "output length", "output cap", "max output"))

    def should_compress(self, session_id: str, current_messages: list[BaseMessage]) -> bool:
        """Determine whether compression should proceed.

        Returns ``False`` (skip compression) if:
        1. Max compression attempts exhausted.
        2. Last two compressions were each ineffective (< 10% reduction).
        """
        gs = self._get_state(session_id)

        if gs.compression_attempts >= self.config.max_compression_attempts:
            logger.info(
                "CompressionGuard: max attempts (%d) reached for session %s.",
                self.config.max_compression_attempts, session_id,
            )
            return False

        if gs.ineffective_count >= self.config.ineffective_threshold:
            logger.info(
                "CompressionGuard: anti-thrashing triggered (%d ineffective) "
                "for session %s.", gs.ineffective_count, session_id,
            )
            return False

        return True

    def record_compression(
        self, session_id: str,
        before_messages: list[BaseMessage],
        after_messages: list[BaseMessage],
    ) -> bool:
        """Record the result of a compression attempt.

        Returns ``True`` if the compression was effective enough to
        continue; ``False`` if it was ineffective.
        """
        gs = self._get_state(session_id)
        gs.compression_attempts += 1

        before_count = len(before_messages)
        after_count = len(after_messages)
        before_tokens = self._estimate_tokens(before_messages)
        after_tokens = self._estimate_tokens(after_messages)

        msg_reduced = after_count < before_count
        token_reduction_pct = (
            (before_tokens - after_tokens) / before_tokens
            if before_tokens > 0 else 0.0
        )
        effective = msg_reduced or token_reduction_pct >= self.config.min_effectiveness_pct

        if not effective:
            gs.ineffective_count += 1
            logger.info(
                "CompressionGuard: compression ineffective (attempt %d, "
                "msg_reduction=%s, token_reduction=%.1f%%) session=%s",
                gs.compression_attempts, msg_reduced, token_reduction_pct * 100,
                session_id,
            )
        else:
            gs.ineffective_count = 0

        gs.last_msg_count = after_count
        gs.last_token_estimate = after_tokens
        self._save_state(session_id, gs)

        return effective

    @override
    async def abefore_agent(
        self, state: AgentState, runtime: Runtime
    ) -> dict[str, Any] | None:
        session_id = self._get_session_id(state)
        state_register_mem.set_state(session_id, _COMPRESSION_GUARD_KEY, CompressionGuardState())
        return None
