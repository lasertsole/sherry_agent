"""humanInTheLoop middleware — orchestrates all HITL layers via middleware hooks.

Delegates to:
- ``detection.py``  — hardline + dangerous pattern matching
- ``approval.py``   — command approval pipeline + smart approval + plugin tool approval
- ``gates.py``       — write gate, interrupt, MCP, kanban, pairing, slash confirm
- ``types.py``       — shared enums, dataclasses, config, langchain stubs
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from langchain_core.messages import ToolCall
from loguru import logger
from langgraph.errors import GraphInterrupt
from runtime.state_register import state_register_mem

from .types import (
    ApprovalResult,
    HITLConfig,
    SmartApprovalResult,
    WriteTarget,
    _STATE_PREFIX,
    BLOCKED_MESSAGE,
    AgentMiddleware,
    AgentState,
    Runtime,
    AIMessage,
    ToolMessage,
    ToolCallRequest,
    ActionRequest,
    ReviewConfig,
    HITLRequest,
    InterruptOnConfig,
    override,
    interrupt,
)
from .approval import ApprovalPipeline, set_session_yolo
from .gates import (
    WriteApprovalGate,
    InterruptManager,
    MCPElicitationConsent,
    KanbanTriage,
    PairingStore,
    SlashConfirm,
)
from .strategies import ApprovalContext, ApprovalHandlerRegistry, ApprovalOutcome


class HumanInTheLoop(AgentMiddleware):
    """Full hermes-agent HITL as a single middleware.

    Hooks used:
    - ``after_model``: intercept tool calls → approval interrupt
    - ``wrap_tool_call`` / ``awrap_tool_call``: gate individual tool execution
    - ``abefore_agent``: reset per-turn state
    """

    def __init__(self, config: HITLConfig | None = None):
        """Initialise the HITL middleware with an optional custom config.

        Instantiates all sub-gates and parses ``interrupted_tools`` from the
        config into :class:`InterruptOnConfig` entries.
        """
        super().__init__()
        self.config = config or HITLConfig()

        self._approval_hooks: list[Callable[[str, ApprovalResult], None]] = []
        self._fire_hooks = self._make_hook_dispatcher()

        self.approval = ApprovalPipeline(self.config, self._fire_hooks)
        self.write_gate = WriteApprovalGate(self.config)
        self.interrupt_mgr = InterruptManager()
        self.mcp_consent = MCPElicitationConsent()
        self.kanban = KanbanTriage(self.config.kanban_recurrence_limit)
        self.pairing = PairingStore()
        self.slash_confirm = SlashConfirm(self.config)

        self._interrupt_on: dict[str, InterruptOnConfig] = {}
        for tool_name, tool_config in self.config.interrupted_tools.items():
            if isinstance(tool_config, bool):
                if tool_config is True:
                    self._interrupt_on[tool_name] = InterruptOnConfig(
                        allowed_decisions=["approve", "edit", "reject"]
                    )
            elif isinstance(tool_config, dict) and tool_config.get("allowed_decisions"):
                self._interrupt_on[tool_name] = tool_config

        self._approval_registry = ApprovalHandlerRegistry()

    # ── Hook registration ────────────────────────────────────────────────

    def register_approval_hook(self, hook: Callable[[str, ApprovalResult], None]):
        """Register an external callback invoked after every approval decision."""
        self._approval_hooks.append(hook)

    def _make_hook_dispatcher(self) -> Callable[[str, ApprovalResult], None]:
        """Build a dispatcher that calls all registered approval hooks safely."""

        def _dispatch(session_id: str, result: ApprovalResult):
            for hook in self._approval_hooks:
                try:
                    hook(session_id, result)
                except Exception:
                    logger.exception("HITL approval hook raised an exception")

        return _dispatch

    # ── Session / state helpers ──────────────────────────────────────────

    @staticmethod
    def _session_id(state: AgentState) -> str:
        """Extract session_id from agent state, defaulting to ``"default"``."""
        sid = state.get("session_id", "")
        return sid.strip() or "default"

    def _get_state(self, session_id: str, key: str, default: Any = None) -> Any:
        """Read a namespaced HITL value from the in-memory state register."""
        return state_register_mem.get_state(session_id, f"{_STATE_PREFIX}:{key}", default)

    def _set_state(self, session_id: str, key: str, value: Any) -> bool:
        """Write a namespaced HITL value to the in-memory state register."""
        return state_register_mem.set_state(session_id, f"{_STATE_PREFIX}:{key}", value)

    # ── Delegating convenience methods ───────────────────────────────────

    def check_command(self, command: str, session_id: str) -> ApprovalResult:
        """Delegate to :meth:`ApprovalPipeline.check_command`."""
        return self.approval.check_command(command, session_id)

    def check_command_with_approval(
        self, command: str, session_id: str, prompt_fn=None
    ) -> ApprovalResult:
        """Delegate to :meth:`ApprovalPipeline.check_command_with_approval`."""
        return self.approval.check_command_with_approval(command, session_id, prompt_fn)

    def smart_approve(self, command: str) -> SmartApprovalResult:
        """Delegate to :meth:`ApprovalPipeline.smart_approve`."""
        return self.approval.smart_approve(command)

    def clarify(
        self, question: str, choices: list[str] | None = None, session_id: str = "default"
    ) -> str | None:
        """Ask the user a clarification question via an interrupt.

        Args:
            question: The question to present to the user.
            choices:  Optional multiple-choice options (max 4, plus "Other").
            session_id: Session context.

        Returns:
            The user's response string if approved, ``None`` if rejected or on error.
        """
        if choices:
            choices = choices[:4] + ["Other (type your answer)"]
        from .types import ActionRequest as AR, ReviewConfig as RC, HITLRequest as HR  # noqa: N817, N814

        hitl_request = HR(
            action_requests=[AR(name="clarify", args={"question": question, "choices": choices})],
            review_configs=[RC(action_name="clarify", allowed_decisions=["approve", "reject"])],
        )
        try:
            response = interrupt(hitl_request)
            decisions = response.get("decisions", [])
            decision_type = decisions[0]["type"] if decisions else ""
            if decision_type == "yolo" and session_id:
                set_session_yolo(session_id)
            if decision_type in ("approve", "yolo"):
                return decisions[0].get("message", "Approved")
            return None
        except Exception:
            logger.exception("Clarify interrupt failed")
            return None

    def request_write(self, target: WriteTarget, content: str, session_id: str) -> ApprovalResult:
        """Delegate to :meth:`WriteApprovalGate.request_write`."""
        return self.write_gate.request_write(target, content, session_id)

    def approve_write(self, session_id: str, write_id: str) -> bool:
        """Delegate to :meth:`WriteApprovalGate.approve_write`."""
        return self.write_gate.approve_write(session_id, write_id)

    def reject_write(self, session_id: str, write_id: str) -> bool:
        """Delegate to :meth:`WriteApprovalGate.reject_write`."""
        return self.write_gate.reject_write(session_id, write_id)

    def get_pending_writes(self, session_id: str, target: WriteTarget | None = None):
        """Delegate to :meth:`WriteApprovalGate.get_pending_writes`."""
        return self.write_gate.get_pending_writes(session_id, target)

    def set_interrupt(self, session_id: str, active: bool = True):
        """Delegate to :meth:`InterruptManager.set_interrupt`."""
        self.interrupt_mgr.set_interrupt(session_id, active)

    def is_interrupted(self, session_id: str) -> bool:
        """Delegate to :meth:`InterruptManager.is_interrupted`."""
        return self.interrupt_mgr.is_interrupted(session_id)

    def clear_interrupt(self, session_id: str):
        """Delegate to :meth:`InterruptManager.clear_interrupt`."""
        self.interrupt_mgr.clear_interrupt(session_id)

    def request_tool_approval(
        self, tool_name: str, tool_args: dict, session_id: str
    ) -> ApprovalResult:
        """Delegate to :meth:`ApprovalPipeline.request_tool_approval`."""
        return self.approval.request_tool_approval(tool_name, tool_args, session_id)

    def approve_tool_for_session(self, tool_name: str, tool_args: dict, session_id: str):
        """Delegate to :meth:`ApprovalPipeline.approve_tool_for_session`."""
        self.approval.approve_tool_for_session(tool_name, tool_args, session_id)

    def request_elicitation_consent(self, server_name: str, session_id: str) -> ApprovalResult:
        """Delegate to :meth:`MCPElicitationConsent.request_consent`."""
        return self.mcp_consent.request_consent(server_name, session_id)

    def report_task_failure(self, task_id: str, session_id: str):
        """Delegate to :meth:`KanbanTriage.report_task_failure`."""
        return self.kanban.report_task_failure(task_id, session_id)

    def resolve_triage(self, task_id: str, session_id: str):
        """Delegate to :meth:`KanbanTriage.resolve_triage`."""
        self.kanban.resolve_triage(task_id, session_id)

    def is_user_allowed(self, platform: str, user_id: str) -> bool:
        """Delegate to :meth:`PairingStore.is_user_allowed`."""
        return self.pairing.is_user_allowed(platform, user_id)

    def approve_user(self, platform: str, user_id: str):
        """Delegate to :meth:`PairingStore.approve_user`."""
        self.pairing.approve_user(platform, user_id)

    def revoke_user(self, platform: str, user_id: str):
        """Delegate to :meth:`PairingStore.revoke_user`."""
        self.pairing.revoke_user(platform, user_id)

    def confirm_destructive(self, action: str, session_id: str) -> ApprovalResult:
        """Delegate to :meth:`SlashConfirm.confirm_destructive`."""
        return self.slash_confirm.confirm_destructive(action, session_id)

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # Middleware hooks
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    def _reset_turn_state(self, state: AgentState) -> None:
        session_id = self._session_id(state)
        self._set_state(session_id, "turn_interrupted", False)

    @override
    def before_agent(self, state: AgentState, runtime: Runtime) -> dict[str, Any] | None:
        self._reset_turn_state(state)
        return None

    @override
    async def abefore_agent(self, state: AgentState, runtime: Runtime) -> dict[str, Any] | None:
        self._reset_turn_state(state)
        return None

    def _sandbox_bypass_interrupt(
        self,
        tool_call: ToolCall,
        tool_name: str,
        action_desc: str = "",
        session_id: str = "",
    ) -> tuple[bool, ToolMessage | None]:
        """Sandbox-bypass approval: gate ``sandbox=False`` tool calls.

        Reuses the ``interrupt(HITLRequest(...))`` template verbatim (the
        resume contract): ``Command(resume={"decisions": [{"type": "approve"}]})``
        proceeds; ``{"type": "reject", "message": ...}`` (or no decision) yields
        a ``"User denied: <msg>. <BLOCKED_MESSAGE>"`` error ToolMessage with NO
        second interrupt. ``GraphInterrupt`` is re-raised, never swallowed.
        A ``{"type": "yolo"}`` decision approves the call AND activates the
        session-scoped YOLO flag (subsequent gates in this session bypass).

        Contract (plan line 739, Metis ruling): the tool layer's scope/policy
        denial is NOT repeated here — ``_deny_sandbox_bypass`` in terminal.py /
        python_repl.py already raises ``ToolException`` for non-main
        ``caller_scope`` + ``sandbox=False`` and for ``SANDBOX_POLICY=required``
        + ``sandbox=False``. YOLO mode (``is_yolo_mode``) and ``sandbox=True``
        calls must never reach the interrupt — callers filter them out BEFORE
        invoking this helper.

        Returns:
            ``(approved, deny_message)``: ``approved=True`` → the caller
            proceeds with the original tool_call; ``approved=False`` →
            ``deny_message`` (an error ToolMessage) must be appended by the caller.
        """
        action_request = ActionRequest(
            name=tool_name,
            args=tool_call.get("args", {}),
            description=(
                "沙箱绕过审批：该调用请求 sandbox=False"
                "（经环境清洗后直接执行，无 OS 沙箱隔离）。"
                + (f" {action_desc}" if action_desc else "")
            ),
        )
        review_config = ReviewConfig(
            action_name=tool_name,
            allowed_decisions=["approve", "reject"],
        )
        try:
            hitl_response = interrupt(
                HITLRequest(
                    action_requests=[action_request],
                    review_configs=[review_config],
                )
            )
            decisions = hitl_response.get("decisions", [])
            decision_type = decisions[0]["type"] if decisions else ""
            if decision_type == "yolo" and session_id:
                set_session_yolo(session_id)
                return True, None
            if decision_type == "approve":
                return True, None
            msg = (
                (decisions[0].get("message") or "Rejected by user") if decisions else "No decision"
            )
            return False, ToolMessage(
                content=f"User denied: {msg}. {BLOCKED_MESSAGE}",
                name=tool_name,
                tool_call_id=tool_call["id"],
                status="error",
            )
        except GraphInterrupt:
            # Real HITL interrupt: let LangGraph persist it so the frontend
            # approval dialog can fire. Do NOT swallow it.
            raise
        except Exception:
            return False, ToolMessage(
                content=f"Approval interrupt failed. {BLOCKED_MESSAGE}",
                name=tool_name,
                tool_call_id=tool_call["id"],
                status="error",
            )

    def after_model(self, state: AgentState, runtime: Runtime) -> dict[str, Any] | None:
        """Intercept tool calls after model output for HITL approval."""
        messages = state.get("messages", [])
        if not messages:
            return None

        last_ai_msg = next((msg for msg in reversed(messages) if isinstance(msg, AIMessage)), None)
        if not last_ai_msg or not getattr(last_ai_msg, "tool_calls", None):
            return None

        outcome = ApprovalOutcome()
        ctx = ApprovalContext(
            mw=self,
            state=state,
            runtime=runtime,
            session_id=self._session_id(state),
            outcome=outcome,
        )
        for tool_call in last_ai_msg.tool_calls:
            self._approval_registry.dispatch(tool_call, ctx)

        last_ai_msg.tool_calls = outcome.revised_tool_calls
        return (
            {"messages": [last_ai_msg, *outcome.artificial_tool_messages]}
            if outcome.artificial_tool_messages
            else None
        )

    @override
    async def aafter_model(self, state: AgentState, runtime: Runtime) -> dict[str, Any] | None:
        return self.after_model(state, runtime)

    @override
    def wrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], ToolMessage],
    ) -> ToolMessage:
        session_id = self._session_id(request.state)
        if self.interrupt_mgr.is_interrupted(session_id):
            tool_name = request.tool_call.get("name", "unknown")
            self.interrupt_mgr.clear_interrupt(session_id)
            return ToolMessage(
                content=f"Tool execution interrupted by user. {BLOCKED_MESSAGE}",
                name=tool_name,
                tool_call_id=request.tool_call["id"],
                status="error",
            )
        return handler(request)

    @override
    async def awrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], Awaitable[ToolMessage]],
    ) -> ToolMessage:
        session_id = self._session_id(request.state)
        if self.interrupt_mgr.is_interrupted(session_id):
            tool_name = request.tool_call.get("name", "unknown")
            self.interrupt_mgr.clear_interrupt(session_id)
            return ToolMessage(
                content=f"Tool execution interrupted by user. {BLOCKED_MESSAGE}",
                name=tool_name,
                tool_call_id=request.tool_call["id"],
                status="error",
            )
        return await handler(request)
