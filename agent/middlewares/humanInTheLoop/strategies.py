"""Strategy handlers for the ``after_model`` tool-approval routing.

Each :class:`ToolApprovalHandler` owns one tool-type branch of
``HumanInTheLoop.after_model`` (terminal command approval, python_repl
sandbox-bypass, memory-write gating, ``interrupt_on`` config tools,
plugin-escalated approval). The :class:`ApprovalHandlerRegistry` walks
them in registration order; a handler that consumes the tool call stops
the walk, a handler returning ``False`` falls through to the remaining
handlers (matching the original if-chain fall-through semantics).
"""

from __future__ import annotations

import json
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any
from collections.abc import Sequence

from langchain_core.messages import ToolCall, ToolMessage
from langgraph.errors import GraphInterrupt

from .approval import is_yolo_mode, set_session_yolo
from .detection import detect_clawhub_command
from .types import (
    ApprovalDecision,
    ApprovalMode,
    ApprovalResult,
    SmartApprovalResult,
    WriteTarget,
    BLOCKED_MESSAGE,
    AgentState,
    Runtime,
    ActionRequest,
    ReviewConfig,
    HITLRequest,
    InterruptOnConfig,
    interrupt,
)

if TYPE_CHECKING:
    from .core import HumanInTheLoop


@dataclass
class ApprovalOutcome:
    """Collects the after_model routing result for the current model turn."""

    revised_tool_calls: list[ToolCall] = field(default_factory=list)
    artificial_tool_messages: list[ToolMessage] = field(default_factory=list)

    def approve(self, tool_call: ToolCall) -> None:
        self.revised_tool_calls.append(tool_call)

    def deny(self, tool_call: ToolCall, tool_name: str, content: str) -> None:
        self.artificial_tool_messages.append(
            ToolMessage(
                content=content,
                name=tool_name,
                tool_call_id=tool_call["id"],
                status="error",
            )
        )


@dataclass
class ApprovalContext:
    """Everything a :class:`ToolApprovalHandler` needs to route one tool call."""

    mw: HumanInTheLoop
    state: AgentState
    runtime: Runtime
    session_id: str
    outcome: ApprovalOutcome


class ToolApprovalHandler(ABC):
    """Strategy for one tool-type branch of the after_model approval route."""

    @abstractmethod
    def matches(self, tool_call: ToolCall, ctx: ApprovalContext) -> bool: ...

    @abstractmethod
    def handle(self, tool_call: ToolCall, ctx: ApprovalContext) -> bool:
        """Route the tool call.

        Returns ``True`` when the call is fully handled (no further
        handlers run); ``False`` falls through to the remaining handlers.
        """
        ...


class TerminalApprovalHandler(ToolApprovalHandler):
    """Terminal tool: command approval pipeline (hardline/dangerous +
    sandbox-bypass + smart approval + dangerous-command interrupt)."""

    def matches(self, tool_call: ToolCall, ctx: ApprovalContext) -> bool:
        return tool_call.get("name", "") == "terminal"

    def handle(self, tool_call: ToolCall, ctx: ApprovalContext) -> bool:
        mw = ctx.mw
        tool_name: str = tool_call.get("name", "")
        tool_args: dict[str, Any] = tool_call.get("args", {})

        command = tool_args.get("commands", "") or tool_args.get("command", "")
        if isinstance(command, list):
            command = " && ".join(command)
        result: ApprovalResult = mw.approval.check_command(command, ctx.session_id)

        if result.blocked and result.decision == ApprovalDecision.DENY:
            ctx.outcome.deny(tool_call, tool_name, result.reason)
            return True

        # Sandbox-bypass approval gates ALL sandbox=False executions —
        # including the dangerous-command interrupt below, whose approve
        # path would otherwise skip the bypass. The tool layer's
        # scope/policy denial is NOT repeated here — _deny_sandbox_bypass
        # in terminal.py owns it and raises ToolException at execution.
        if not is_yolo_mode(mw.config, ctx.session_id) and not tool_args.get("sandbox", True):
            approved, deny_msg = mw._sandbox_bypass_interrupt(
                tool_call, tool_name, f"Command: {command}", ctx.session_id
            )
            if not approved:
                if deny_msg is not None:
                    ctx.outcome.artificial_tool_messages.append(deny_msg)
                return True
            # Approved bypass → the human explicitly approved THIS call
            # (full args shown); skip smart approval and the
            # dangerous-command re-prompt.
            ctx.outcome.approve(tool_call)
            return True

        # clawhub remote-npm execution always requires explicit human
        # confirmation — SMART auto-approval and the benign-command fast path
        # must not bypass the gate.
        clawhub_tag = detect_clawhub_command(command)

        # Smart approval (layer 6)
        if not result.approved and mw.config.mode == ApprovalMode.SMART and not clawhub_tag:
            smart = mw.approval.smart_approve(command)
            if smart == SmartApprovalResult.APPROVE:
                ctx.outcome.approve(tool_call)
                return True
            if smart == SmartApprovalResult.DENY:
                ctx.outcome.deny(tool_call, tool_name, f"Smart approval denied. {BLOCKED_MESSAGE}")
                return True

        # If still not approved — or the call is clawhub remote execution —
        # use interrupt for human decision.
        if (clawhub_tag or not result.approved) and not is_yolo_mode(mw.config, ctx.session_id):
            description = (
                f"clawhub remote npm execution ({clawhub_tag}): {command}"
                if clawhub_tag
                else f"Dangerous command: {command}"
            )
            action_request = ActionRequest(
                name=tool_name,
                args=tool_args,
                description=description,
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
                if decision_type == "yolo":
                    set_session_yolo(ctx.session_id)
                    ctx.outcome.approve(tool_call)
                elif decision_type == "approve":
                    ctx.outcome.approve(tool_call)
                else:
                    msg = (
                        (decisions[0].get("message") or "Rejected by user")
                        if decisions
                        else "No decision"
                    )
                    ctx.outcome.deny(tool_call, tool_name, f"User denied: {msg}. {BLOCKED_MESSAGE}")
            except GraphInterrupt:
                # Real HITL interrupt: let LangGraph persist it so the
                # frontend approval dialog can fire. Do NOT swallow it.
                raise
            except Exception:
                ctx.outcome.deny(
                    tool_call, tool_name, f"Approval interrupt failed. {BLOCKED_MESSAGE}"
                )
            return True

        ctx.outcome.approve(tool_call)
        return True


class SandboxBypassApprovalHandler(ToolApprovalHandler):
    """python_repl: sandbox-bypass approval for ``sandbox=False`` calls.

    Same gate as terminal: human approval unless YOLO is active. Approved
    bypasses FALL THROUGH to the remaining handlers (plugin allow-through)
    — preserving the contract that python_repl is otherwise NOT
    intercepted. The tool layer's scope/policy denial is owned by
    _deny_sandbox_bypass in python_repl.py.
    """

    def matches(self, tool_call: ToolCall, ctx: ApprovalContext) -> bool:
        mw = ctx.mw
        return (
            tool_call.get("name", "") == "python_repl"
            and not is_yolo_mode(mw.config, ctx.session_id)
            and not tool_call.get("args", {}).get("sandbox", True)
        )

    def handle(self, tool_call: ToolCall, ctx: ApprovalContext) -> bool:
        tool_name: str = tool_call.get("name", "")
        approved, deny_msg = ctx.mw._sandbox_bypass_interrupt(
            tool_call,
            tool_name,
            f"Query: {tool_call.get('args', {}).get('query', '')}",
            ctx.session_id,
        )
        if not approved:
            if deny_msg is not None:
                ctx.outcome.artificial_tool_messages.append(deny_msg)
            return True
        # Approved → fall through (no append): the plugin allow-through
        # layer keeps pass-through semantics.
        return False


class MemoryWriteApprovalHandler(ToolApprovalHandler):
    """Memory tool: write approval gate for add/replace actions."""

    def matches(self, tool_call: ToolCall, ctx: ApprovalContext) -> bool:
        return tool_call.get("name", "") == "memory" and ctx.mw.config.write_approval_memory

    def handle(self, tool_call: ToolCall, ctx: ApprovalContext) -> bool:
        tool_name: str = tool_call.get("name", "")
        tool_args: dict[str, Any] = tool_call.get("args", {})
        action = tool_args.get("action", "")
        if action not in ("add", "replace"):
            return False

        write_result = ctx.mw.write_gate.request_write(
            WriteTarget.MEMORY,
            json.dumps(tool_args),
            ctx.session_id,
        )
        if write_result.blocked:
            ctx.outcome.deny(tool_call, tool_name, write_result.reason)
            return True
        return False


class InterruptOnApprovalHandler(ToolApprovalHandler):
    """Configured ``interrupt_on`` tools: interrupt with per-tool allowed
    decisions (approve / edit / reject)."""

    def matches(self, tool_call: ToolCall, ctx: ApprovalContext) -> bool:
        return tool_call.get("name", "") in ctx.mw._interrupt_on

    def handle(self, tool_call: ToolCall, ctx: ApprovalContext) -> bool:
        mw = ctx.mw
        tool_name: str = tool_call.get("name", "")
        tool_args: dict[str, Any] = tool_call.get("args", {})
        config: InterruptOnConfig = mw._interrupt_on[tool_name]
        description_value = config.get("description")
        if callable(description_value):
            description = description_value(tool_call, ctx.state, ctx.runtime)
        elif description_value is not None:
            description = description_value
        else:
            description = f"{mw.config.description_prefix}\n\nTool: {tool_name}\nArgs: {tool_args}"

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
                ctx.outcome.deny(tool_call, tool_name, f"No decision received. {BLOCKED_MESSAGE}")
                return True

            decision = decisions[0]
            allowed = config["allowed_decisions"]
            if decision["type"] == "yolo":
                set_session_yolo(ctx.session_id)
                ctx.outcome.approve(tool_call)
            elif decision["type"] == "approve" and "approve" in allowed:
                ctx.outcome.approve(tool_call)
            elif decision["type"] == "edit" and "edit" in allowed:
                edited = decision.get("edited_action", {})
                revised_tc: Any = dict(tool_call)
                revised_tc["args"] = edited.get("args", tool_args)
                revised_tc["name"] = edited.get("name", tool_name)
                ctx.outcome.approve(revised_tc)
            elif decision["type"] == "reject" and "reject" in allowed:
                msg = decision.get("message", f"User rejected {tool_name}")
                ctx.outcome.deny(tool_call, tool_name, f"{msg}. {BLOCKED_MESSAGE}")
            else:
                ctx.outcome.deny(
                    tool_call, tool_name, f"Unexpected decision type. {BLOCKED_MESSAGE}"
                )
        except GraphInterrupt:
            # Real HITL interrupt: let LangGraph persist it so the
            # frontend approval dialog can fire. Do NOT swallow it.
            raise
        except Exception:
            ctx.outcome.deny(tool_call, tool_name, f"Approval interrupt failed. {BLOCKED_MESSAGE}")
        return True


class PluginToolApprovalHandler(ToolApprovalHandler):
    """Plugin-escalated tool approval (layer 10) — catch-all for every
    tool call not consumed by an earlier handler."""

    def matches(self, tool_call: ToolCall, ctx: ApprovalContext) -> bool:
        return True

    def handle(self, tool_call: ToolCall, ctx: ApprovalContext) -> bool:
        tool_name: str = tool_call.get("name", "")
        tool_args: dict[str, Any] = tool_call.get("args", {})
        tool_approval = ctx.mw.approval.request_tool_approval(tool_name, tool_args, ctx.session_id)
        if tool_approval.blocked:
            ctx.outcome.deny(tool_call, tool_name, tool_approval.reason)
            return True
        ctx.outcome.approve(tool_call)
        return True


class ApprovalHandlerRegistry:
    """Ordered chain of tool-approval strategies.

    ``dispatch`` walks the handlers in registration order: the first
    handler that consumes the tool call ends the walk; a ``False`` return
    falls through to the next matching handler.
    """

    def __init__(self, handlers: Sequence[ToolApprovalHandler] | None = None):
        if handlers is None:
            handlers = (
                TerminalApprovalHandler(),
                SandboxBypassApprovalHandler(),
                MemoryWriteApprovalHandler(),
                InterruptOnApprovalHandler(),
                PluginToolApprovalHandler(),
            )
        self._handlers: list[ToolApprovalHandler] = list(handlers)

    def register(self, handler: ToolApprovalHandler) -> None:
        self._handlers.append(handler)

    def dispatch(self, tool_call: ToolCall, ctx: ApprovalContext) -> bool:
        """Route one tool call through the chain.

        Returns ``True`` when a handler consumed the call.
        """
        for handler in self._handlers:
            if handler.matches(tool_call, ctx) and handler.handle(tool_call, ctx):
                return True
        return False
