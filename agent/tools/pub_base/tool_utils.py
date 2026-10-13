"""Shared tool helper utilities (error shape + the session-id argument).

The session id every session-aware tool takes from graph state is declared ONCE
here: 25 tool modules used to repeat the identical
``SessionId = Annotated[str, InjectedState("session_id")]`` line, so a change to
the injection contract had 25 places to find.
"""

import json
from typing import Annotated, Any

from loguru import logger

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


def resolve_tool_session_id(session_id: str, run_manager: Any) -> str:
    """Session id for a tool call: the injected value, else the run config.

    ``session_id`` reaches a tool through ``InjectedState`` in production, but
    direct callers (tests, cron, scripted probes) pass the empty default — the
    run manager's ``config["configurable"]`` is the fallback then, and an empty
    result stays fail-closed (no HITL approval possible).
    """
    return session_id or extract_session_id(run_manager)


def extract_session_id(*sources: Any, require: bool = False) -> str:
    """Resolve a session id from any of the shapes a session id travels in.

    The ONE implementation behind two façades that used to duplicate this logic:

    * ``path_utils._extract_session_id(run_manager)`` — the tool path (reads the
      runnable config), empty string when absent (fail-closed: no session id
      means no HITL approval is possible);
    * ``RepetitionGuardWrapper._extract_session_id(input_, config)`` — the graph
      wrapper path (also reads a plain input dict and ``Command(resume=...)``),
      raising when absent.

    Sources are probed in order: an object exposing ``.config`` (a
    ``CallbackManagerForToolRun``), a plain input dict, a LangGraph
    ``Command``'s resume value, then any plain config mapping.
    """
    for source in sources:
        if source is None:
            continue
        config = getattr(source, "config", None)
        candidates: list[Any] = []
        if isinstance(config, dict):
            candidates.append(config)
        if isinstance(source, dict):
            candidates.append(source)
        else:
            try:
                from langgraph.types import Command

                if isinstance(source, Command):
                    resume = getattr(source, "resume", None)
                    if isinstance(resume, dict):
                        candidates.append(resume)
            except Exception:
                logger.debug("session-id probe: Command.resume lookup failed", exc_info=True)
        for candidate in candidates:
            session_id = candidate.get("session_id", "")
            if isinstance(session_id, str) and session_id.strip():
                return session_id
            configurable = candidate.get("configurable")
            if isinstance(configurable, dict):
                session_id = configurable.get("session_id", "")
                if isinstance(session_id, str) and session_id.strip():
                    return session_id
    if require:
        raise RuntimeError(
            "session_id is required but not found in the run manager, the input, "
            "Command.resume or config.configurable"
        )
    return ""


__all__ = ["SessionId", "extract_session_id", "resolve_tool_session_id", "tool_error"]
