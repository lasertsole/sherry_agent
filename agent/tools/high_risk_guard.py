"""``high_risk_guard`` — the agent-facing high-risk keyword / operation manager.

Both the main agent and subagents may call it. Registering a keyword makes later
calls whose argument text contains it ask a human (main) or bubble
``[NEEDS_APPROVAL]`` out of a child; ``register_op`` writes a preventive ledger
entry so an exact ``(tool, args)`` call is refused before it ever runs.

The panel and this tool share one store
(:mod:`agent.tools.pub_base.high_risk_guard_store`). Keywords are
substring-matched case-insensitively and must be at least three characters.
"""

from __future__ import annotations

from typing import Literal

from langchain_core.tools import BaseTool, tool
from pydantic import BaseModel, Field

from agent.tools.pub_base import SessionId
from loguru import logger
from agent.tools.pub_base.high_risk_guard_store import (
    args_hash,
    default_store,
    validate_keyword,
)


class HighRiskGuardInput(BaseModel):
    """Arguments for ``high_risk_guard``."""

    action: Literal[
        "add_keyword",
        "list_keywords",
        "remove_keyword",
        "set_policy",
        "reset_keyword",
        "register_op",
        "list_ops",
        "remove_op",
    ] = Field(description="The operation to perform.")
    keyword: str = Field(default="", description="Keyword (add/remove/set_policy/reset).")
    scope: Literal["session", "global"] = Field(
        default="session",
        description="Where a keyword is registered: this session (default) or globally.",
    )
    policy: Literal["block", "allow"] = Field(
        default="block", description="block = scan/ask; allow = never scan this keyword."
    )
    tool_name: str = Field(default="", description="Tool name for register_op.")
    args: dict = Field(default_factory=dict, description="Exact call arguments for register_op.")
    result_summary: str = Field(
        default="", description="Result summary recorded for register_op (shown on a replay)."
    )
    op_id: int | None = Field(default=None, description="Ledger row id for remove_op.")
    session_id: SessionId = ""


def _format_keywords(entries: list[dict]) -> str:
    lines = [f"High-risk keywords ({len(entries)}):"]
    for entry in entries:
        marker = "EXECUTED" if entry.get("executed") else entry.get("policy", "block")
        scope = entry.get("scope") or "builtin"
        lines.append(f"- {entry['keyword']} [{marker}, {scope}, {entry.get('source', '')}]")
    return "\n".join(lines)


@tool("high_risk_guard", args_schema=HighRiskGuardInput)
def high_risk_guard_tool(
    action: str,
    keyword: str = "",
    scope: str = "session",
    policy: str = "block",
    tool_name: str = "",
    args: dict | None = None,
    result_summary: str = "",
    op_id: int | None = None,
    session_id: SessionId = "",
) -> str:
    """Manage high-risk keywords and the executed-operation ledger.

    - add_keyword: register a keyword (>= 3 characters) whose appearance in a
      tool call requires human approval. Repeating an approved operation is
      then refused with its first result.
    - list_keywords: every built-in and custom keyword with its policy/state.
    - remove_keyword / set_policy / reset_keyword: remove, switch block↔allow,
      or clear the executed marker (a reset allows one more run).
    - register_op / list_ops / remove_op: preventive ledger entries for an exact
      tool call, the recorded executions, and their deletion.
    """
    store = default_store()
    session = str(session_id or "").strip() or "default"
    # A child's registrations belong to the ROOT main session: the panel, the
    # parent's scans and a later step's approval all read that one scope.
    try:
        from runtime import StateKey, state_register_mem

        parent = state_register_mem.get_state(session, StateKey.APPROVAL_SCOPE_PARENT, None)
        if isinstance(parent, str) and parent.strip():
            session = parent.strip()
    except Exception:  # noqa: BLE001 - a missing register keeps the given session
        logger.debug("high_risk_guard: approval scope lookup failed", exc_info=True)

    if action == "add_keyword":
        error = validate_keyword(keyword)
        if error is not None:
            return f"Error: {error}"
        ok, message = store.add_keyword(
            session, keyword, source="agent", policy=policy, scope=scope
        )
        return message if ok else f"Error: {message}"

    if action == "list_keywords":
        return _format_keywords(store.list_keywords(session))

    if action == "remove_keyword":
        ok, message = store.remove_keyword(session, keyword.strip())
        return message if ok else f"Error: {message}"

    if action == "set_policy":
        ok, message = store.set_policy(session, keyword.strip(), policy)
        return message if ok else f"Error: {message}"

    if action == "reset_keyword":
        if store.reset_executed(session, keyword.strip()):
            return f"keyword '{keyword.strip()}' reset (one more execution allowed)"
        return f"Error: keyword '{keyword.strip()}' has no executed marker"

    if action == "register_op":
        name = str(tool_name or "").strip()
        if not name:
            return "Error: tool_name is required for register_op"
        store.record_op(session, name, args or {}, result_summary or "(registered by the agent)")
        return (
            f"operation registered: tool={name}, args_hash={args_hash(name, args or {})}; "
            "an exact repeat will be refused"
        )

    if action == "list_ops":
        entries = store.list_ops(session)
        if not entries:
            return "High-risk operations ledger is empty."
        lines = [f"High-risk operations ({len(entries)}):"]
        for entry in entries:
            lines.append(
                f"- #{entry['id']} {entry['tool_name']} {entry['args_preview'][:120]} "
                f"| {entry['result_summary'][:120]}"
            )
        return "\n".join(lines)

    if action == "remove_op":
        if op_id is None:
            return "Error: op_id is required for remove_op"
        return (
            f"ledger entry #{op_id} removed"
            if store.remove_op(session, int(op_id))
            else f"Error: unknown op_id {op_id}"
        )

    return f"Error: unknown action {action!r}"


def build_high_risk_guard_tool() -> BaseTool:
    """Build the tool (main + children) with its safety metadata."""
    high_risk_guard_tool.metadata = {
        "idempotent": True,
        "side_effect_scope": "session",
        "high_risk": False,
    }
    return high_risk_guard_tool
