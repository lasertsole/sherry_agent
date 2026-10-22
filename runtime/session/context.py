"""The session currently executing in THIS task (contextvar).

The server's ``turn_scope`` sets it around a turn; agent-side singletons such
as ``MemoryStore`` read it when a tool has no session parameter. It is reset on
scope exit and is task-local, so concurrent sessions never share the value.
"""

from __future__ import annotations

from contextvars import ContextVar, Token

_current_session_id: ContextVar[str] = ContextVar("sherry_current_session_id", default="")


def get_current_session_id() -> str:
    """The session id bound to this task, or "" outside a turn."""
    return _current_session_id.get()


def set_current_session_id(session_id: str) -> Token[str]:
    """Bind *session_id* for the current task; pair with ``reset``."""
    return _current_session_id.set(str(session_id or ""))


def reset_current_session_id(token: Token[str]) -> None:
    """Restore the previous binding."""
    _current_session_id.reset(token)
