"""The high-risk keyword/ledger gate, shared by every execution path.

The middleware chain is not the only place tools run: PTC dispatches script
calls straight into ``tool.ainvoke`` (``agent/tools/ptc/rpc_server.py``) and an
LDW leaf is a bare ``create_agent`` graph with no middleware at all. Those
paths used to bypass the guard entirely. This module is the ONE implementation
of the decision, extracted verbatim from ``ToolGuardrails._high_risk_gate`` so
the middleware and the two replay paths cannot drift apart.

Callers own the surface:

* ``ToolGuardrails`` wraps the returned refusal in a ``ToolMessage``;
* the PTC RPC server records it in its ``calls_log`` and returns ``ok: false``;
* the LDW leaf's guarded tool face returns it as the tool's result text.

The gate is deliberately side-effect free on refusal and only records AFTER a
handler returned. It never touches the network, the model or the sandbox, so a
broken guard can be treated as "no opinion" (the replay call sites fail open).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from loguru import logger

from config.features import HIGH_RISK_GUARD

from .high_risk_guard_store import (
    HighRiskGuardStore,
    args_hash,
    default_store,
    scan_high_risk_keywords,
)
from .high_risk_policy import should_ledger, should_scan_keywords

__all__ = [
    "HighRiskDecision",
    "MCP_PREFIX",
    "check_high_risk_call",
    "guard_enabled",
    "hitl_flags",
    "is_child_session",
    "keyword_approved",
    "record_high_risk_intent",
    "record_high_risk_result",
    "replay_path_gate_enabled",
    "resolve_approval_scope",
]

MCP_PREFIX = "mcp__"

#: Appended to a refusal produced inside an execute channel that has no human
#: to ask (a PTC script). It tells the model how to reach the approval card.
PTC_REFUSAL_NOTE = (
    "A PTC script has no approval channel: make this call as a normal tool "
    "call so the approval card can appear."
)


@dataclass(frozen=True)
class HighRiskDecision:
    """The outcome of one gate evaluation.

    ``refusal`` is the exact text the caller must surface (or ``None`` to
    proceed); ``hit_keyword`` is the keyword whose scan matched (recorded as
    executed after a successful call); ``scope`` is the session whose ledger
    and approvals this call shares.
    """

    refusal: str | None = None
    hit_keyword: str | None = None
    scope: str = ""
    ledgered: bool = False
    scanned: bool = False


def guard_enabled() -> bool:
    """Whether the guard is enabled at all (the existing global switch)."""
    return bool(HIGH_RISK_GUARD["enabled"])


def replay_path_gate_enabled() -> bool:
    """Whether the replay paths (PTC / LDW leaf / compile check) are gated.

    The escape hatch: ``False`` restores the pre-replay-gate behaviour without
    a code rollback, while the middleware chain keeps its guard.
    """
    return guard_enabled() and bool(HIGH_RISK_GUARD["replay_path_gate_enabled"])


def is_child_session(session_id: str) -> bool:
    """A subagent session key (the shape every spawn uses)."""
    return ":subagent:" in str(session_id or "")


def resolve_approval_scope(session_id: str) -> str:
    """The session whose approvals/ledger this call shares (root for a child)."""
    try:
        from runtime import StateKey, state_register_mem

        parent = state_register_mem.get_state(session_id, StateKey.APPROVAL_SCOPE_PARENT, None)
        if isinstance(parent, str) and parent.strip():
            return parent.strip()
    except Exception:  # noqa: BLE001 - a missing register keeps the given session
        logger.debug("high-risk gate: approval scope lookup failed", exc_info=True)
    return session_id


def hitl_flags(session_id: str) -> tuple[bool, bool]:
    """(yolo, confirm_all) for the session; fail-open to (False, False)."""
    try:
        from agent.middlewares.humanInTheLoop.approval import (
            is_confirm_all_mode,
            is_yolo_mode,
        )
        from agent.middlewares.humanInTheLoop.types import HITLConfig

        config = HITLConfig()
        return is_yolo_mode(config, session_id), is_confirm_all_mode(config, session_id)
    except Exception:  # noqa: BLE001 - the guard must never break a turn
        logger.debug("high-risk gate: HITL flag lookup failed", exc_info=True)
        return False, False


def keyword_approved(
    scope: str, session_id: str, tool_name: str, args: dict[str, Any], keyword: str
) -> bool:
    """Whether a human already approved this keyword+tool(+args) call."""
    digest = args_hash(tool_name, args)
    try:
        from runtime import StateKey, state_register_mem
    except Exception:  # noqa: BLE001 - no register means no approvals
        return False
    for owner in (scope, session_id):
        try:
            approvals = state_register_mem.get_state(owner, StateKey.KEYWORD_APPROVALS, []) or []
        except Exception:  # noqa: BLE001 - fail-open to "not approved"
            continue
        if not isinstance(approvals, list):
            continue
        for entry in approvals:
            if not isinstance(entry, dict):
                continue
            if str(entry.get("keyword") or "").lower() != keyword.lower():
                continue
            entry_tool = str(entry.get("tool") or "")
            if entry_tool and entry_tool != tool_name:
                continue
            entry_hash = str(entry.get("args_hash") or "")
            if entry_hash and entry_hash != digest:
                continue
            return True
    return False


def _blocking_text(reason: str, tool_name: str, row: dict[str, Any]) -> str:
    """The refusal text for one blocking ledger row."""
    summary = str(row.get("result_summary") or "")[:200]
    if reason == "in_flight":
        return (
            f"[HIGH-RISK GUARD] This exact operation is still in progress in this "
            f"session (tool={tool_name}). Re-running is refused while it is in flight."
        )
    if reason == "unknown":
        return (
            f"[HIGH-RISK GUARD] A previous attempt of this exact operation "
            f"(tool={tool_name}) was interrupted before it reported; its side effects "
            "are unknown. Check the external state, then delete the ledger entry in "
            "the high-risk panel to run it again."
        )
    return (
        f"[NON-IDEMPOTENT GUARD] This exact operation already ran in this "
        f"session (tool={tool_name}). First result: {summary}. Re-running is "
        "refused; delete the ledger entry in the high-risk panel if it really "
        "must run again."
    )


def check_high_risk_call(
    *,
    session_id: str,
    scope: str,
    tool_name: str,
    tool: Any,
    args: dict[str, Any] | None,
    note: str = "",
    store: HighRiskGuardStore | None = None,
) -> HighRiskDecision:
    """Apply the full guard decision table to one call.

    Order (identical to the middleware's): metadata short-circuit → ledger
    (committed / in-flight / unknown) → keyword scan → executed marker →
    recorded approval → YOLO → confirm_all. ``note`` is appended to a refusal
    (the PTC call site uses it to explain the missing approval channel).
    """
    if not guard_enabled():
        return HighRiskDecision()

    call_args: dict[str, Any] = args if isinstance(args, dict) else {}
    ledgered = should_ledger(tool, call_args)
    scanned = should_scan_keywords(tool)
    if not ledgered and not scanned:
        # The metadata short-circuit: ordinary tools never touch the database.
        return HighRiskDecision()

    resolved_scope = str(scope or "").strip() or resolve_approval_scope(session_id)
    store = store or default_store()
    suffix = f"\n{note}" if note else ""

    if ledgered:
        blocking = store.check_blocking(resolved_scope, tool_name, call_args)
        if blocking is not None:
            reason, row = blocking
            return HighRiskDecision(
                refusal=_blocking_text(reason, tool_name, row) + suffix,
                scope=resolved_scope,
                ledgered=True,
                scanned=scanned,
            )

    hit_keyword: str | None = None
    if scanned:
        blockers = store.load_active_blockers(resolved_scope)
        hit = scan_high_risk_keywords(
            json.dumps(call_args, ensure_ascii=False, default=str), blockers
        )
        if hit is not None:
            hit_keyword = hit.keyword
            entry = store.get_keyword(resolved_scope, hit.keyword)
            if entry is not None and entry.get("executed"):
                summary = str(entry.get("result_summary") or "")[:200]
                return HighRiskDecision(
                    refusal=(
                        f"[HIGH-RISK GUARD] The keyword '{hit.keyword}' operation already "
                        f"ran in this session. First result: {summary}. A repeat is "
                        "refused; delete the keyword's executed marker in the high-risk "
                        "panel if it really must run again." + suffix
                    ),
                    scope=resolved_scope,
                    hit_keyword=hit.keyword,
                    ledgered=ledgered,
                    scanned=True,
                )
            approved = keyword_approved(
                resolved_scope, session_id, tool_name, call_args, hit.keyword
            )
            yolo, confirm_all = hitl_flags(session_id)
            if not approved and yolo and (not ledgered or is_child_session(session_id)):
                # full_access: reversible tools (and children, which carry no
                # HITL) are trusted; the ledger above still blocks repeats.
                approved = True
            if not approved:
                if confirm_all and is_child_session(session_id):
                    return HighRiskDecision(
                        refusal=(
                            "[BLOCKED: confirm_all mode] This operation needs approval, "
                            "but the session runs in confirm-before-changes mode and a "
                            "subagent has no approval channel." + suffix
                        ),
                        scope=resolved_scope,
                        hit_keyword=hit.keyword,
                        ledgered=ledgered,
                        scanned=True,
                    )
                return HighRiskDecision(
                    refusal=(
                        f"[NEEDS_APPROVAL: keyword={hit.keyword}, tool={tool_name}]\n"
                        f"This operation contains the high-risk keyword '{hit.keyword}'.\n"
                        "If you are a subagent: include this [NEEDS_APPROVAL] block "
                        "verbatim in your final result so the parent agent can ask the user.\n"
                        "If you are the main agent: use the `question` tool to ask the "
                        "user; after approval resume the step with approval_context (or "
                        "retry the call — the approval is recorded for this session)." + suffix
                    ),
                    scope=resolved_scope,
                    hit_keyword=hit.keyword,
                    ledgered=ledgered,
                    scanned=True,
                )

    return HighRiskDecision(
        refusal=None,
        hit_keyword=hit_keyword,
        scope=resolved_scope,
        ledgered=ledgered,
        scanned=scanned,
    )


def record_high_risk_intent(
    *,
    scope: str,
    tool_name: str,
    tool: Any,
    args: dict[str, Any] | None,
    store: HighRiskGuardStore | None = None,
) -> None:
    """Write the in-flight marker BEFORE the handler runs (ledgered tools only).

    Fail-open: a store failure just means the crash window stays open, never
    that the call is refused or fails.
    """
    if not scope:
        return
    call_args: dict[str, Any] = args if isinstance(args, dict) else {}
    if not should_ledger(tool, call_args):
        return
    try:
        (store or default_store()).record_intent(scope, tool_name, call_args)
    except Exception:  # noqa: BLE001 - the marker must never fail the call
        logger.warning("high-risk guard: writing the in-flight marker failed", exc_info=True)


def record_high_risk_result(
    *,
    scope: str,
    tool_name: str,
    tool: Any,
    args: dict[str, Any] | None,
    result_text: str,
    status: str,
    hit_keyword: str | None,
    store: HighRiskGuardStore | None = None,
) -> None:
    """Record one execution: commit/fail the ledger row + mark the keyword.

    ``status`` is the tool result's own status string (``"error"`` means the
    tool reported a failure). An error marks the in-flight row ``failed``,
    which never blocks a retry; a success commits it. Every store failure is
    swallowed — recording must never fail the turn.
    """
    if not scope:
        return
    error = str(status or "").strip().lower() == "error"
    call_args: dict[str, Any] = args if isinstance(args, dict) else {}
    if not should_ledger(tool, call_args) and not hit_keyword:
        # Nothing to record: an ordinary tool never constructs the store.
        return
    store = store or default_store()
    try:
        if should_ledger(tool, call_args):
            if error:
                store.mark_failed(scope, tool_name, call_args)
            else:
                store.commit_op(scope, tool_name, call_args, result_text)
        if hit_keyword and not error:
            store.mark_executed(scope, hit_keyword, result_text)
    except Exception:  # noqa: BLE001 - recording must never fail the turn
        logger.warning("high-risk guard: recording the execution failed", exc_info=True)
