"""Default tool-result injector builders."""

from __future__ import annotations

from collections import defaultdict
from typing import Any

from langchain_core.messages import ToolMessage

__all__ = [
    "build_hook_context",
    "clear_hook_context",
    "stash_hook_context",
]

#: Hook ``additionalContext`` texts waiting for the next tool result in a
#: session. ``UserHooksMiddleware`` is inner to the enrichment middleware, so
#: it stashes while the handler runs; the injector drains immediately after.
_PENDING_HOOK_CONTEXT: dict[str, list[str]] = defaultdict(list)


def stash_hook_context(session_id: str, text: str) -> None:
    """Queue one PostToolUse hook's ``additionalContext`` for the result."""
    if session_id and isinstance(text, str) and text.strip():
        _PENDING_HOOK_CONTEXT[session_id].append(text)


def build_hook_context(
    tool_name: str,
    tool_args: dict[str, Any],
    result: ToolMessage,
    session_id: str,
) -> str | None:
    """Drain pending hook context into the tool result the model sees."""
    del tool_name, tool_args, result
    texts = _PENDING_HOOK_CONTEXT.pop(session_id, None)
    if not texts:
        return None
    return "\n".join(f"[Hook context]: {text}" for text in texts)


def clear_hook_context(session_id: str) -> None:
    """Drop a session's pending hook context (session teardown, tests)."""
    _PENDING_HOOK_CONTEXT.pop(session_id, None)
