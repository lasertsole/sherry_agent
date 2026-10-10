"""Shared tool helper utilities (error shape + the session-id argument).

The session id every session-aware tool takes from graph state is declared ONCE
here: 25 tool modules used to repeat the identical
``SessionId = Annotated[str, InjectedState("session_id")]`` line, so a change to
the injection contract had 25 places to find.
"""

import json
from typing import Annotated

from langgraph.prebuilt.tool_node import InjectedState

#: The injected session id argument every session-aware tool declares.
SessionId = Annotated[str, InjectedState("session_id")]


def tool_error(message, **extra) -> str:
    """Return a JSON error string for tool handlers.

    >>> tool_error("file not found")
    '{"error": "file not found"}'
    >>> tool_error("bad input", success=False)
    '{"error": "bad input", "success": false}'
    """
    result = {"error": str(message)}
    if extra:
        result.update(extra)
    return json.dumps(result, ensure_ascii=False)


__all__ = ["SessionId", "tool_error"]
