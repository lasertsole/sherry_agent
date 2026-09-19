"""Operator scope for persistent tool approvals (P2-2).

Approval decisions belong to a human operator: an approval made by operator A
never authorizes operator B. Sherry's WebSocket transport carries no
authenticated user identity, so the HITL layer uses the **session identity** as
the operator-scope equivalent; a transport that does know the human (a channel
adapter, a future authenticated gateway) can set an explicit operator with
:func:`operator_scope`.

A turn whose triggering message is a *system injection* (cron / heartbeat /
task-intent / subagent-completion carrier — ``metadata.internal`` truthy or
``metadata.origin == "cron"``) has **no operator**: nobody is present to answer
an approval prompt, so approval-requiring gates are auto-denied instead of
suspending the graph indefinitely.
"""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar, Token
from typing import Any
from collections.abc import Iterator

# ContextVar default sentinel: distinguishes "no scope was ever set" (fall back
# to turn/session resolution) from an explicit ``None`` scope (no operator).
_UNSET: object = object()

_operator_var: ContextVar[object] = ContextVar("hitl_tool_approval_operator", default=_UNSET)

NO_OPERATOR_MESSAGE = "No human operator is present to approve this action, so it was auto-denied."


def set_operator(operator: str | None) -> Token[object]:
    """Set the active approval operator (``None`` = explicitly nobody present).

    Returns the context token for :func:`reset_operator`.
    """
    return _operator_var.set(operator)


def reset_operator(token: Token[object]) -> None:
    """Restore the approval operator that was active before :func:`set_operator`."""
    _operator_var.reset(token)


def current_operator() -> str | None:
    """Return the explicitly scoped operator, or ``None`` when no scope is set."""
    value = _operator_var.get()
    if value is _UNSET:
        return None
    return value if isinstance(value, str) else None


@contextmanager
def operator_scope(operator: str | None) -> Iterator[None]:
    """Run a block with *operator* as the active approval scope."""
    token = set_operator(operator)
    try:
        yield
    finally:
        reset_operator(token)


def _is_headless_turn(state: dict[str, Any]) -> bool:
    """True when the turn's triggering message is a system injection.

    Only the **last** human message decides: a later real user message makes
    the turn human-driven again even if an internal injection preceded it.
    """
    messages = state.get("messages") or []
    for msg in reversed(messages):
        if getattr(msg, "type", "") != "human":
            continue
        meta = getattr(msg, "metadata", None) or {}
        if str(meta.get("origin") or "").strip() == "cron":
            return True
        return bool(meta.get("internal"))
    return False


def resolve_turn_operator(state: dict[str, Any], session_id: str = "default") -> str | None:
    """Resolve the approval operator for *state* (``None`` = nobody present).

    Priority: an explicitly scoped operator (ContextVar) wins; then a headless
    system-injected turn resolves to ``None``; otherwise the session identity
    is the operator-scope equivalent used for isolation.
    """
    scoped = _operator_var.get()
    if scoped is not _UNSET:
        return scoped if isinstance(scoped, str) else None
    if _is_headless_turn(state):
        return None
    return session_id.strip() or "default"
