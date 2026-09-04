"""humanInTheLoop middleware — orchestrates all HITL layers via middleware hooks.

Delegates to:
- ``detection.py``  — hardline + dangerous pattern matching
- ``approval.py``   — command approval pipeline + smart approval + plugin tool approval
- ``gates.py``       — write gate, interrupt, MCP, kanban, pairing, slash confirm
- ``types.py``       — shared enums, dataclasses, config, langchain stubs
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from typing import Any

from langchain_core.messages import ToolCall
from loguru import logger
from langgraph.errors import GraphInterrupt
from runtime.state_register import state_register_mem

from .types import (
    ApprovalDecision,
    ApprovalMode,
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
from .approval import ApprovalPipeline, is_yolo_mode
from .gates import (
    WriteApprovalGate,
    InterruptManager,
    MCPElicitationConsent,
    KanbanTriage,
    PairingStore,
    SlashConfirm,
)


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
        from .types import ActionRequest as AR, ReviewConfig as RC, HITLRequest as HR

        hitl_request = HR(
            action_requests=[AR(name="clarify", args={"question": question, "choices": choices})],
            review_configs=[RC(action_name="clarify", allowed_decisions=["approve", "reject"])],
        )
        try:
            response = interrupt(hitl_request)
            decisions = response.get("decisions", [])
            if decisions and decisions[0]["type"] == "approve":
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
    ) -> tuple[bool, ToolMessage | None]:
        """Sandbox-bypass approval (Task 8): gate ``sandbox=False`` tool calls.

        Reuses the ``interrupt(HITLRequest(...))`` template verbatim (the Task 5
        resume contract): ``Command(resume={"decisions": [{"type": "approve"}]})``
        proceeds; ``{"type": "reject", "message": ...}`` (or no decision) yields
        a ``"User denied: <msg>. <BLOCKED_MESSAGE>"`` error ToolMessage with NO
        second interrupt. ``GraphInterrupt`` is re-raised, never swallowed.

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
            if decisions and decisions[0]["type"] == "approve":
                return True, None
            msg = (
                (decisions[0].get("message") or "Rejected by user")
                if decisions
                else "No decision"
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

        session_id = self._session_id(state)
        revised_tool_calls: list[ToolCall] = []
        artificial_tool_messages: list[ToolMessage] = []

        for tool_call in last_ai_msg.tool_calls:
            tool_name: str = tool_call.get("name", "")
            tool_args: dict[str, Any] = tool_call.get("args", {})

            # ── Terminal tool: command approval pipeline ──
            if tool_name == "terminal":
                command = tool_args.get("commands", "") or tool_args.get("command", "")
                if isinstance(command, list):
                    command = " && ".join(command)
                result = self.approval.check_command(command, session_id)

                if result.blocked and result.decision == ApprovalDecision.DENY:
                    artificial_tool_messages.append(
                        ToolMessage(
                            content=result.reason,
                            name=tool_name,
                            tool_call_id=tool_call["id"],
                            status="error",
                        )
                    )
                    continue

                # ── Sandbox-bypass approval (Task 8) ──
                # Gate sandbox=False terminal calls: human approval required
                # unless YOLO is active. Inserted BEFORE smart approval so it
                # gates ALL sandbox=False executions — including the
                # dangerous-command interrupt below, whose approve path
                # (``continue`` inside try) would otherwise skip the bypass.
                # NOTE: the tool layer's scope/policy denial (subagent /
                # background caller_scope + SANDBOX_POLICY=required, both with
                # sandbox=False) is NOT repeated here — _deny_sandbox_bypass
                # in terminal.py owns it and raises ToolException at execution.
                if not is_yolo_mode(self.config) and not tool_args.get("sandbox", True):
                    approved, deny_msg = self._sandbox_bypass_interrupt(
                        tool_call, tool_name, f"Command: {command}"
                    )
                    if not approved:
                        if deny_msg is not None:
                            artificial_tool_messages.append(deny_msg)
                        continue
                    # Approved bypass → the human explicitly approved THIS
                    # call (full args shown); skip smart approval and the
                    # dangerous-command re-prompt.
                    revised_tool_calls.append(tool_call)
                    continue

                # Smart approval (layer 6)
                if not result.approved and self.config.mode == ApprovalMode.SMART:
                    smart = self.approval.smart_approve(command)
                    if smart == SmartApprovalResult.APPROVE:
                        revised_tool_calls.append(tool_call)
                        continue
                    elif smart == SmartApprovalResult.DENY:
                        artificial_tool_messages.append(
                            ToolMessage(
                                content=f"Smart approval denied. {BLOCKED_MESSAGE}",
                                name=tool_name,
                                tool_call_id=tool_call["id"],
                                status="error",
                            )
                        )
                        continue

                # If still not approved, use interrupt for human decision
                if not result.approved:
                    action_request = ActionRequest(
                        name=tool_name,
                        args=tool_args,
                        description=f"Dangerous command: {command}",
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
                        if decisions and decisions[0]["type"] == "approve":
                            revised_tool_calls.append(tool_call)
                        else:
                            msg = (
                                (decisions[0].get("message") or "Rejected by user")
                                if decisions
                                else "No decision"
                            )
                            artificial_tool_messages.append(
                                ToolMessage(
                                    content=f"User denied: {msg}. {BLOCKED_MESSAGE}",
                                    name=tool_name,
                                    tool_call_id=tool_call["id"],
                                    status="error",
                                )
                            )
                    except GraphInterrupt:
                        # Real HITL interrupt: let LangGraph persist it so the
                        # frontend approval dialog can fire. Do NOT swallow it.
                        raise
                    except Exception:
                        artificial_tool_messages.append(
                            ToolMessage(
                                content=f"Approval interrupt failed. {BLOCKED_MESSAGE}",
                                name=tool_name,
                                tool_call_id=tool_call["id"],
                                status="error",
                            )
                        )
                    continue

                revised_tool_calls.append(tool_call)
                continue

            # ── python_repl: sandbox-bypass approval (Task 8) ──
            # Same gate as terminal: human approval for sandbox=False calls
            # unless YOLO is active. Approved bypasses FALL THROUGH to the
            # remaining gates (plugin allow-through) — preserving the Task 5
            # contract that python_repl is otherwise NOT intercepted. The
            # tool layer's scope/policy denial is NOT repeated here (owned by
            # _deny_sandbox_bypass in python_repl.py).
            if (
                tool_name == "python_repl"
                and not is_yolo_mode(self.config)
                and not tool_args.get("sandbox", True)
            ):
                approved, deny_msg = self._sandbox_bypass_interrupt(
                    tool_call, tool_name, f"Query: {tool_args.get('query', '')}"
                )
                if not approved:
                    if deny_msg is not None:
                        artificial_tool_messages.append(deny_msg)
                    continue
                # Approved → fall through (no append+continue): the plugin
                # allow-through layer keeps Task 5 pass-through semantics.

            # ── Memory tool: write approval gate ──
            if tool_name == "memory" and self.config.write_approval_memory:
                action = tool_args.get("action", "")
                if action in ("add", "replace"):
                    write_result = self.write_gate.request_write(
                        WriteTarget.MEMORY,
                        json.dumps(tool_args),
                        session_id,
                    )
                    if write_result.blocked:
                        artificial_tool_messages.append(
                            ToolMessage(
                                content=write_result.reason,
                                name=tool_name,
                                tool_call_id=tool_call["id"],
                                status="error",
                            )
                        )
                        continue

            # ── Configured interrupt_on tools ──
            if tool_name in self._interrupt_on:
                config = self._interrupt_on[tool_name]
                description_value = config.get("description")
                if callable(description_value):
                    description = description_value(tool_call, state, runtime)
                elif description_value is not None:
                    description = description_value
                else:
                    description = (
                        f"{self.config.description_prefix}\n\nTool: {tool_name}\nArgs: {tool_args}"
                    )

                action_request = ActionRequest(
                    name=tool_name,
                    args=tool_args,
                    description=description,
                )
                review_config = ReviewConfig(
                    action_name=tool_name,
                    allowed_decisions=config["allowed_decisions"],
                )
                try:
                    hitl_response = interrupt(
                        HITLRequest(
                            action_requests=[action_request],
                            review_configs=[review_config],
                        )
                    )
                    decisions = hitl_response.get("decisions", [])
                    if not decisions:
                        artificial_tool_messages.append(
                            ToolMessage(
                                content=f"No decision received. {BLOCKED_MESSAGE}",
                                name=tool_name,
                                tool_call_id=tool_call["id"],
                                status="error",
                            )
                        )
                        continue

                    decision = decisions[0]
                    allowed = config["allowed_decisions"]
                    if decision["type"] == "approve" and "approve" in allowed:
                        revised_tool_calls.append(tool_call)
                    elif decision["type"] == "edit" and "edit" in allowed:
                        edited = decision.get("edited_action", {})
                        revised_tc: Any = dict(tool_call)
                        revised_tc["args"] = edited.get("args", tool_args)
                        revised_tc["name"] = edited.get("name", tool_name)
                        revised_tool_calls.append(revised_tc)
                    elif decision["type"] == "reject" and "reject" in allowed:
                        msg = decision.get("message", f"User rejected {tool_name}")
                        artificial_tool_messages.append(
                            ToolMessage(
                                content=f"{msg}. {BLOCKED_MESSAGE}",
                                name=tool_name,
                                tool_call_id=tool_call["id"],
                                status="error",
                            )
                        )
                    else:
                        artificial_tool_messages.append(
                            ToolMessage(
                                content=f"Unexpected decision type. {BLOCKED_MESSAGE}",
                                name=tool_name,
                                tool_call_id=tool_call["id"],
                                status="error",
                            )
                        )
                except GraphInterrupt:
                    # Real HITL interrupt: let LangGraph persist it so the
                    # frontend approval dialog can fire. Do NOT swallow it.
                    raise
                except Exception:
                    artificial_tool_messages.append(
                        ToolMessage(
                            content=f"Approval interrupt failed. {BLOCKED_MESSAGE}",
                            name=tool_name,
                            tool_call_id=tool_call["id"],
                            status="error",
                        )
                    )
                continue

            # ── Plugin-escalated tool approval (layer 10) ──
            tool_approval = self.approval.request_tool_approval(tool_name, tool_args, session_id)
            if tool_approval.blocked:
                artificial_tool_messages.append(
                    ToolMessage(
                        content=tool_approval.reason,
                        name=tool_name,
                        tool_call_id=tool_call["id"],
                        status="error",
                    )
                )
                continue

            revised_tool_calls.append(tool_call)

        last_ai_msg.tool_calls = revised_tool_calls
        return (
            {"messages": [last_ai_msg, *artificial_tool_messages]}
            if artificial_tool_messages
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
