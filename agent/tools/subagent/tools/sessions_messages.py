"""Read a subagent's checkpoint transcript by its session key.

Backs the ``sessions_messages`` LLM tool: after a subagent times out or is
killed, the caller holds only the child's ``session_key`` (from the spawn
result) and whatever partial ``result_text`` the announce carried — the last
assistant text, never the tool-call history. The child's LangGraph checkpoint
holds that history and survives the run (``_release_child_resources`` only
closes the checkpointer connection; child cleanup never calls
``delete_thread_history``), so this module reads it back through
``agent.state_port.read_messages`` — the same thread-id derivation
(``rand_str_to_int(session_key)``) the child wrote it with.

The output is token-budgeted on purpose: a child may have run dozens of
iterations, and replaying them verbatim would flood the caller's window.
"""

from __future__ import annotations

from typing import Any

from langchain_core.messages import BaseMessage
from loguru import logger

from config.features import TOOLS_TIMEOUTS

#: Messages fetched when the caller does not ask for a count, and the schema
#: bounds the tool clamps ``limit`` to. ``limit`` decides how many messages are
#: FETCHED; the token budgets decide how much is SHOWN.
DEFAULT_LIMIT = 10
MIN_LIMIT = 1
MAX_LIMIT = 30


def clamp_limit(limit: Any) -> int:
    """Clamp a model-provided ``limit`` into ``[MIN_LIMIT, MAX_LIMIT]``."""
    try:
        value = int(limit)
    except (TypeError, ValueError):
        return DEFAULT_LIMIT
    if value < MIN_LIMIT:
        return MIN_LIMIT
    return min(value, MAX_LIMIT)


def check_read_permission(caller_session_id: str, target_session_key: str) -> tuple[bool, str]:
    """Whether the caller may read the target session's transcript.

    The rule is the spawner (or the run's completion owner) — the same "who
    controls this run" dimension ``sessions_send`` uses — plus a session
    reading its own transcript. A blank caller is refused: the caller's
    identity is injected state, so blank means a leaked/misbuilt tool.
    """
    from ..registry import get_run_by_child_session_key
    from ..spawn.privilege import resolve_caller_session_key

    caller_key = resolve_caller_session_key(caller_session_id)
    if not caller_key:
        return False, "unknown calling session"
    target = str(target_session_key or "").strip()
    if not target:
        return False, "target_session_key is required"
    if target == caller_key:
        return True, ""
    run = get_run_by_child_session_key(target)
    if run is None:
        return False, f"no subagent run found for session key {target}"
    if caller_key in (run.spawned_by, run.requester_session_key):
        return True, ""
    return False, "not the spawning session"


async def read_child_messages(
    child_session_key: str, limit: int, graph: Any | None = None
) -> list[BaseMessage]:
    """The child's last *limit* checkpoint messages, in chronological order.

    An empty list means the checkpoint is empty (the child never ran) or was
    purged (``clear_session``); both read the same to the caller. ``graph`` is
    the same test seam ``state_port.read_messages`` exposes.
    """
    from agent.state_port import read_messages

    messages = await read_messages(session_id=child_session_key, graph=graph)
    return list(messages)[-limit:]


def message_text(message: BaseMessage) -> str:
    """One-line rendering of a message: text plus its tool calls.

    Tool calls are the reason this tool exists — a child's last assistant text
    often says only "let me modify the function", while the interesting content
    is which tool it called with which arguments and what came back.
    """
    from pub.func.extract_text_from_content import extract_text_from_content

    parts: list[str] = []
    text = extract_text_from_content(getattr(message, "content", ""))
    if text:
        parts.append(text.strip())
    tool_calls = getattr(message, "tool_calls", None) or []
    if tool_calls:
        rendered = "; ".join(
            f"{call.get('name')}({_compact_args(call.get('args'))})"
            for call in tool_calls
            if isinstance(call, dict)
        )
        if rendered:
            parts.append(f"tool_calls: {rendered}")
    name = str(getattr(message, "name", "") or "")
    if name and not parts:
        parts.append(f"tool_name: {name}")
    return " ".join(parts)


def _compact_args(args: Any) -> str:
    """Compact ``k=v`` rendering of a tool call's arguments, bounded."""
    if not isinstance(args, dict) or not args:
        return ""
    parts: list[str] = []
    for key, value in args.items():
        text = str(value).replace("\n", " ")
        if len(text) > 120:
            text = text[:120] + "…"
        parts.append(f"{key}={text}")
    return ", ".join(parts)


def render_messages(messages: list[BaseMessage], session_key: str) -> str:
    """Render the transcript, per-message and total bounded by token budgets."""
    from pub.func.message.token_truncation import truncate_by_tokens

    per_message = TOOLS_TIMEOUTS["session_messages_per_message_token_budget"]
    lines = [
        f"## Subagent session: {session_key}",
        f"Showing the last {len(messages)} message(s), oldest first:",
        "",
    ]
    for message in messages:
        role = str(getattr(message, "type", "") or "?")
        rendered = truncate_by_tokens(message_text(message), max_tokens=per_message)
        lines.append(f"[{role}] {rendered}")
    return truncate_by_tokens(
        "\n".join(lines), max_tokens=TOOLS_TIMEOUTS["session_messages_token_budget"]
    )


async def read_session_transcript(
    caller_session_id: str,
    target_session_key: str,
    limit: Any = DEFAULT_LIMIT,
    query: str | None = None,
) -> str:
    """The tool's whole contract, as a string (never raises)."""
    allowed, reason = check_read_permission(caller_session_id, target_session_key)
    if not allowed:
        return f"Cannot read session: {reason}"

    target = str(target_session_key).strip()
    bounded = clamp_limit(limit)
    try:
        messages = await read_child_messages(target, bounded)
    except Exception:  # noqa: BLE001 - the tool boundary reports it as text
        logger.exception("sessions_messages: checkpoint read failed for {}", target)
        return f"Cannot read session: checkpoint read failed for {target}"

    if not messages:
        return (
            f"No messages found for session {target} "
            "(the checkpoint may be empty or the session was purged)."
        )

    keyword = str(query or "").strip().lower()
    if keyword:
        messages = [m for m in messages if keyword in message_text(m).lower()]
        if not messages:
            return f"No message in session {target} matches {query!r}."

    return render_messages(messages, target)


__all__ = [
    "DEFAULT_LIMIT",
    "MAX_LIMIT",
    "MIN_LIMIT",
    "check_read_permission",
    "clamp_limit",
    "message_text",
    "read_child_messages",
    "read_session_transcript",
    "render_messages",
]
