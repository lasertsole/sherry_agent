"""Tool-call loop detection and circuit breaking.

Equivalent to hermes-agent's ``agent/tool_guardrails.py``.

Detects three distinct tool-loop pathologies and can warn or hard-stop:

1. **Exact failure repetition** — same tool + same arguments failing repeatedly.
2. **Same-tool failure accumulation** — same tool failing with different args.
3. **Idempotent no-progress** — read-only tool returning identical results repeatedly.

Decision actions: ``allow`` → ``warn`` → ``block`` → ``halt``.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Awaitable

from loguru import logger
from langgraph.runtime import Runtime
from langgraph.typing import ContextT
from langgraph.prebuilt.tool_node import ToolCallRequest
from typing_extensions import override
from langchain_core.messages import ToolMessage
from langchain.agents.middleware import AgentMiddleware, AgentState

from runtime import state_register_mem


class GuardrailAction(str, Enum):
    ALLOW = "allow"
    WARN = "warn"
    BLOCK = "block"
    HALT = "halt"


@dataclass
class ToolCallGuardrailConfig:
    warnings_enabled: bool = True
    hard_stop_enabled: bool = False
    exact_failure_warn_after: int = 2
    exact_failure_block_after: int = 5
    same_tool_failure_warn_after: int = 3
    same_tool_failure_halt_after: int = 8
    no_progress_warn_after: int = 2
    no_progress_block_after: int = 5


@dataclass
class _ToolCallRecord:
    name: str
    args_hash: str
    is_error: bool
    result_hash: str | None = None


@dataclass
class _TurnGuardrailState:
    records: list[_ToolCallRecord] = field(default_factory=list)
    exact_failure_counts: dict[str, int] = field(default_factory=dict)
    same_tool_failure_counts: dict[str, int] = field(default_factory=dict)
    no_progress_counts: dict[str, int] = field(default_factory=dict)
    blocked_tools: set[str] = field(default_factory=set)
    halt_decision: GuardrailAction | None = None


_GUARDRAIL_STATE_KEY = "tool_guardrail_state"


class ToolGuardrails(AgentMiddleware):
    """Detect and break tool-call loops.

    Parameters
    ----------
    config : ToolCallGuardrailConfig
        Tuning thresholds.  See :class:`ToolCallGuardrailConfig` defaults.
    """

    def __init__(
        self,
        config: ToolCallGuardrailConfig | None = None,
    ):
        super().__init__()
        self.config = config or ToolCallGuardrailConfig()

    def _is_idempotent(self, tool_name: str, tool: Any) -> bool:
        if tool is not None and isinstance(getattr(tool, "metadata", None), dict):
            if tool.metadata.get("idempotent") is not None:
                return bool(tool.metadata["idempotent"])
        return False

    def _get_session_id(self, state: dict[str, Any]) -> str:
        session_id: str = state.get("session_id", "")
        if not session_id.strip():
            raise RuntimeError("ToolGuardrails: session_id is required")
        return session_id

    def _get_state(self, session_id: str) -> _TurnGuardrailState:
        return state_register_mem.get_state(session_id, _GUARDRAIL_STATE_KEY, _TurnGuardrailState())

    def _save_state(self, session_id: str, state: _TurnGuardrailState) -> None:
        state_register_mem.set_state(session_id, _GUARDRAIL_STATE_KEY, state)

    @staticmethod
    def _args_hash(args: dict[str, Any]) -> str:
        try:
            serialized = json.dumps(args, sort_keys=True, default=str)
        except (TypeError, ValueError):
            serialized = str(args)
        return hashlib.md5(serialized.encode()).hexdigest()

    @staticmethod
    def _result_hash(content: str) -> str:
        return hashlib.md5(content.encode()).hexdigest()

    def _evaluate(
        self, gs: _TurnGuardrailState, tool_name: str, args_hash: str, result_hash: str | None, is_error: bool, is_idempotent: bool
    ) -> GuardrailAction:
        if gs.halt_decision is not None:
            return GuardrailAction.HALT

        if tool_name in gs.blocked_tools:
            return GuardrailAction.BLOCK

        action = GuardrailAction.ALLOW

        if is_error:
            exact_key = f"{tool_name}:{args_hash}"
            gs.exact_failure_counts[exact_key] = gs.exact_failure_counts.get(exact_key, 0) + 1
            exact_count = gs.exact_failure_counts[exact_key]

            if self.config.hard_stop_enabled and exact_count >= self.config.exact_failure_block_after:
                action = GuardrailAction.HALT
            elif exact_count >= self.config.exact_failure_block_after:
                action = GuardrailAction.BLOCK
            elif self.config.warnings_enabled and exact_count >= self.config.exact_failure_warn_after:
                action = GuardrailAction.WARN

            gs.same_tool_failure_counts[tool_name] = gs.same_tool_failure_counts.get(tool_name, 0) + 1
            same_count = gs.same_tool_failure_counts[tool_name]

            if self.config.hard_stop_enabled and same_count >= self.config.same_tool_failure_halt_after:
                action = GuardrailAction.HALT
            elif same_count >= self.config.same_tool_failure_halt_after:
                action = GuardrailAction.BLOCK
            elif self.config.warnings_enabled and same_count >= self.config.same_tool_failure_warn_after and action == GuardrailAction.ALLOW:
                action = GuardrailAction.WARN
        else:
            if is_idempotent and result_hash is not None:
                for rec in reversed(gs.records):
                    if rec.name == tool_name and rec.result_hash == result_hash:
                        no_progress_key = f"{tool_name}:{result_hash}"
                        gs.no_progress_counts[no_progress_key] = gs.no_progress_counts.get(no_progress_key, 0) + 1
                        np_count = gs.no_progress_counts[no_progress_key]

                        if self.config.hard_stop_enabled and np_count >= self.config.no_progress_block_after:
                            action = GuardrailAction.HALT
                        elif np_count >= self.config.no_progress_block_after:
                            action = GuardrailAction.BLOCK
                        elif self.config.warnings_enabled and np_count >= self.config.no_progress_warn_after:
                            action = GuardrailAction.WARN
                        break

        if action == GuardrailAction.HALT:
            gs.halt_decision = action
        elif action == GuardrailAction.BLOCK:
            gs.blocked_tools.add(tool_name)

        return action

    @staticmethod
    def _warning_message(tool_name: str, pathology: str, count: int, limit: int) -> str:
        return (
            f"⚠ Tool [{tool_name}] {pathology} detected ({count}/{limit}). "
            "Consider using a different approach or tool. "
            "If you keep repeating, this tool may be blocked."
        )

    @staticmethod
    def _block_message(tool_name: str, pathology: str, count: int, limit: int) -> str:
        return (
            f"🚫 Tool [{tool_name}] has been BLOCKED due to {pathology} "
            f"({count} occurrences, limit: {limit}). "
            "Execution skipped. You MUST use a different approach."
        )

    @staticmethod
    def _halt_message(tool_name: str, pathology: str) -> str:
        return (
            f"🔴 Agent halted: tool [{tool_name}] triggered circuit breaker "
            f"due to {pathology}. The entire turn is being terminated "
            "to prevent an infinite loop."
        )

    @override
    async def abefore_agent(
        self, state: AgentState, runtime: Runtime[ContextT]
    ) -> dict[str, Any] | None:
        session_id = self._get_session_id(state)
        state_register_mem.set_state(session_id, _GUARDRAIL_STATE_KEY, _TurnGuardrailState())
        return None

    @override
    async def awrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], Awaitable[ToolMessage]],
    ) -> ToolMessage:
        session_id = self._get_session_id(request.state)
        gs = self._get_state(session_id)
        tool_name: str = request.tool_call.get("name", "unknown")
        tool_args: dict[str, Any] = request.tool_call.get("args", {})
        args_hash = self._args_hash(tool_args)

        is_idempotent = self._is_idempotent(tool_name, request.tool)

        if gs.halt_decision is not None:
            return ToolMessage(
                content=self._halt_message(tool_name, "previous halt"),
                tool_call_id=request.tool_call["id"],
                name=tool_name,
                status="error",
            )

        if tool_name in gs.blocked_tools:
            return ToolMessage(
                content=self._block_message(tool_name, "previously blocked", 0, 0),
                tool_call_id=request.tool_call["id"],
                name=tool_name,
                status="error",
            )

        result: ToolMessage = await handler(request)

        is_error = getattr(result, "status", None) == "error"
        result_content = str(result.content) if result.content else ""
        result_hash = self._result_hash(result_content) if not is_error and is_idempotent else None

        gs.records.append(_ToolCallRecord(
            name=tool_name,
            args_hash=args_hash,
            is_error=is_error,
            result_hash=result_hash,
        ))

        action = self._evaluate(gs, tool_name, args_hash, result_hash, is_error, is_idempotent)
        self._save_state(session_id, gs)

        if action == GuardrailAction.HALT:
            halt_msg = self._halt_message(tool_name, "excessive repetition")
            logger.error("ToolGuardrails HALT: session=%s tool=%s", session_id, tool_name)
            return ToolMessage(
                content=halt_msg,
                tool_call_id=request.tool_call["id"],
                name=tool_name,
                status="error",
            )

        if action == GuardrailAction.BLOCK:
            if is_error:
                exact_key = f"{tool_name}:{args_hash}"
                exact_count = gs.exact_failure_counts.get(exact_key, 0)
                same_count = gs.same_tool_failure_counts.get(tool_name, 0)
                if same_count >= self.config.same_tool_failure_halt_after:
                    pathology = "same-tool failure accumulation"
                    limit = self.config.same_tool_failure_halt_after
                    count = same_count
                else:
                    pathology = "exact failure repetition"
                    limit = self.config.exact_failure_block_after
                    count = exact_count
            else:
                pathology = "idempotent no-progress"
                no_progress_key = f"{tool_name}:{result_hash}" if result_hash else ""
                count = gs.no_progress_counts.get(no_progress_key, 0)
                limit = self.config.no_progress_block_after

            return ToolMessage(
                content=self._block_message(tool_name, pathology, count, limit),
                tool_call_id=request.tool_call["id"],
                name=tool_name,
                status="error",
            )

        if action == GuardrailAction.WARN:
            if is_error:
                exact_key = f"{tool_name}:{args_hash}"
                exact_count = gs.exact_failure_counts.get(exact_key, 0)
                same_count = gs.same_tool_failure_counts.get(tool_name, 0)

                if exact_count >= self.config.exact_failure_warn_after:
                    warning = self._warning_message(
                        tool_name, "exact failure repetition",
                        exact_count, self.config.exact_failure_block_after,
                    )
                else:
                    warning = self._warning_message(
                        tool_name, "same-tool failure accumulation",
                        same_count, self.config.same_tool_failure_halt_after,
                    )
            else:
                no_progress_key = f"{tool_name}:{result_hash}" if result_hash else ""
                np_count = gs.no_progress_counts.get(no_progress_key, 0)
                warning = self._warning_message(
                    tool_name, "idempotent no-progress",
                    np_count, self.config.no_progress_block_after,
                )

            result = ToolMessage(
                content=f"{result_content}\n\n{warning}",
                tool_call_id=result.tool_call_id,
                name=result.name,
                status=result.status,
            )

        return result
