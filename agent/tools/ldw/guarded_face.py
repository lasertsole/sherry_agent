"""Guarded tool face for agent graphs built outside the middleware chain.

An LDW leaf is a bare ``create_agent`` graph (no ``ToolGuardrails``, no
child chain), yet it carries the same tool face as a spawned child — including
``terminal`` / ``python_repl`` for an executor and every MCP tool for a
``general`` leaf. That made the leaf the largest path where an irreversible
call could run with no keyword scan and no operation ledger.

This wrapper puts the SAME gate (``high_risk_gate.check_high_risk_call``) in
front of each tool, without adding a whole middleware chain to the leaf:

* the tool's ``name`` / ``description`` / ``args_schema`` / ``metadata`` are
  preserved verbatim, so role policy, structured output and the PTC tool map
  keep behaving exactly as before;
* ordinary tools short-circuit on metadata and never touch the ledger;
* a refusal is returned as the tool's result text, which the leaf's ReAct loop
  reads like any other tool error. A failed leaf step then follows LDW's
  existing failure path (auto-degrade to TaskFlow, where normal children carry
  the full HITL flow).
"""

from __future__ import annotations

import asyncio
from typing import Any

from langchain_core.messages import ToolMessage
from langchain_core.tools import BaseTool, StructuredTool
from loguru import logger

from agent.tools.pub_base.high_risk_gate import (
    check_high_risk_call,
    record_high_risk_result,
    replay_path_gate_enabled,
    resolve_approval_scope,
)

__all__ = ["guarded_tool_face"]


def guarded_tool_face(tools: list[Any], session_id: str) -> list[Any]:
    """Wrap every real tool with the shared high-risk gate.

    Returns the SAME list when the replay-path gate is switched off, so the
    escape hatch restores the pre-change graph byte-for-byte. Tools that are
    not ``BaseTool`` instances (test doubles) and tools without an argument
    schema are passed through untouched: the wrapper cannot faithfully
    reproduce their calling convention, and the guard must never be the reason
    a tool stops working.
    """
    if not replay_path_gate_enabled():
        return list(tools)
    scope = resolve_approval_scope(session_id)
    return [_wrap_one(tool, session_id, scope) for tool in tools]


def _wrap_one(tool: Any, session_id: str, scope: str) -> Any:
    if not isinstance(tool, BaseTool) or not getattr(tool, "args_schema", None):
        return tool
    metadata = dict(getattr(tool, "metadata", None) or {})

    async def _acall(**kwargs: Any) -> Any:
        return await _guarded_call(tool, kwargs, session_id, scope)

    def _call(**kwargs: Any) -> Any:
        return asyncio.run(_acall(**kwargs))

    try:
        return StructuredTool.from_function(
            func=_call,
            coroutine=_acall,
            name=tool.name,
            description=getattr(tool, "description", "") or "",
            args_schema=tool.args_schema,
            metadata=metadata or None,
        )
    except Exception:  # noqa: BLE001 - a wrapper failure must not remove a tool
        logger.warning("LDW leaf: could not guard tool {}; passing it through", tool.name)
        return tool


async def _guarded_call(tool: BaseTool, args: dict[str, Any], session_id: str, scope: str) -> Any:
    """Run one leaf tool call behind the gate, then record the outcome."""
    decision = None
    try:
        decision = check_high_risk_call(
            session_id=session_id,
            scope=scope,
            tool_name=tool.name,
            tool=tool,
            args=args,
        )
    except Exception:  # noqa: BLE001 - fail-open, never fail the leaf
        logger.warning("LDW leaf high-risk gate failed; allowing the call", exc_info=True)
    if decision is not None and decision.refusal is not None:
        return decision.refusal

    result = await tool.ainvoke(args, config={"configurable": {"session_id": session_id}})
    is_error = _is_error_result(result)
    content = result.content if isinstance(result, ToolMessage) else result
    record_high_risk_result(
        scope=scope,
        tool_name=tool.name,
        tool=tool,
        args=args,
        result_text=content if isinstance(content, str) else str(content),
        status="error" if is_error else "success",
        hit_keyword=decision.hit_keyword if decision is not None else None,
    )
    return content


def _is_error_result(result: Any) -> bool:
    """True when a tool returned an error envelope instead of raising."""
    if getattr(result, "status", None) == "error":
        return True
    return isinstance(result, dict) and str(result.get("status") or "").lower() == "error"
