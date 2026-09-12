"""Session continuity: inject the previous session's end state into a new session.

Reference: oh-my-openagent session binding + opencode-dev Context Epoch.

Difference from P1-4 Recall:
- P1-4 Recall: fuzzy keyword search over historical conversations.
- LT-8 continuity: exact injection of the previous session's tail summary.

End state is persisted by ``server.DAO.messages.clear_session`` right BEFORE it
deletes the session, and read back by
``workspace.prompt_builder._build_continuity_block`` on the next session start.
Every function here is fail-open: a storage or lookup failure degrades to
``None`` / ``""`` / ``[]`` and never breaks the caller.
"""

import json
import time
from pathlib import Path

from loguru import logger

from config import SRC_DIR

# Persisted end-state files: src/data/session_continuity/{safe-key}.json.
_CONTINUITY_DIR: Path = SRC_DIR / "data" / "session_continuity"

# Tail AI reply is clipped to this many characters before persistence/injection.
_MAX_SUMMARY_CHARS = 500


def save_session_end_state(
    session_id: str,
    channel_id: str,
    chat_id: str,
    summary: str,
    taskflow_ids: list[str],
) -> None:
    """Persist the end state of a session for the next session's continuity.

    Keyed by ``channel_id:chat_id`` when both are available, otherwise by
    ``session_id`` as the degraded fallback. Fail-open: write errors are logged
    and swallowed.
    """
    try:
        _CONTINUITY_DIR.mkdir(parents=True, exist_ok=True)
        key = f"{channel_id}:{chat_id}" if channel_id and chat_id else session_id
        state = {
            "last_session_id": session_id,
            "ended_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "ended_ts": time.time(),
            "summary": summary,
            "taskflow_ids": taskflow_ids,
        }
        path = _CONTINUITY_DIR / f"{_safe_filename(key)}.json"
        path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as e:
        logger.warning("Failed to save session continuity state: {}", e)


def get_last_session_state(channel_id: str, chat_id: str) -> dict | None:
    """Read the last saved end state for a ``channel_id:chat_id`` pair.

    Returns ``None`` when the pair is incomplete, no state file exists, the
    file is unreadable, or its payload is not a JSON object (fail-open).
    """
    key = f"{channel_id}:{chat_id}" if channel_id and chat_id else ""
    if not key:
        return None
    path = _CONTINUITY_DIR / f"{_safe_filename(key)}.json"
    if not path.exists():
        return None
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        logger.warning("Failed to read session continuity state for {}: {}", key, e)
        return None
    return state if isinstance(state, dict) else None


def build_continuity_prompt(session_id: str) -> str:
    """Build the cross-session continuity block for a newly started session.

    Resolves the channel/chat bound to ``session_id``, reads the previous
    session's end state, and renders its tail summary plus related TaskFlow ids.
    A session's own state is never re-injected. Returns "" when unavailable or
    on any failure (fail-open).
    """
    try:
        channel_id, chat_id = _get_channel_chat_for_session(session_id)
        if not channel_id or not chat_id:
            return ""

        state = get_last_session_state(channel_id, chat_id)
        if not state:
            return ""

        # Never inject the current session's own end state.
        if state.get("last_session_id") == session_id:
            return ""

        parts = ["## Last Session (continuity)"]
        summary = state.get("summary")
        if summary:
            parts.append(f"Last conversation ended with: {str(summary)[:_MAX_SUMMARY_CHARS]}")
        taskflow_ids = state.get("taskflow_ids")
        if taskflow_ids:
            parts.append(f"Related tasks: {', '.join(str(t) for t in taskflow_ids[:3])}")
        parts.append(
            "If the user says 'continue' or doesn't specify a new task, "
            "refer to the above context first."
        )
        return "\n".join(parts)
    except Exception:
        return ""


async def auto_save_on_session_end(session_id: str) -> None:
    """Extract a session's tail AI reply and persist its end state.

    Called by ``clear_session`` BEFORE any deletion, so the tail messages still
    exist at extraction time. Fail-open: the caller's cleanup is never blocked.
    """
    try:
        from context_engine.store.core import get_messages_by_lastest_n_turns

        channel_id, chat_id = _get_channel_chat_for_session(session_id)

        # Synchronous store read: the SQLite store is stdlib sqlite3 and the
        # latest-turns helper is not awaitable.
        messages = get_messages_by_lastest_n_turns(session_id, last_n=3)

        # Find the last AI reply (rows come back newest turn first).
        last_ai: str | None = None
        for msg in reversed(messages):
            if not isinstance(msg, dict):
                continue
            if str(msg.get("role", "")) not in ("ai", "assistant"):
                continue
            content = msg.get("content", "")
            if isinstance(content, list):
                content = " ".join(
                    str(block.get("text", ""))
                    for block in content
                    if isinstance(block, dict) and block.get("type") == "text"
                )
            if not isinstance(content, str):
                content = ""
            content = content.strip()
            last_ai = content[:_MAX_SUMMARY_CHARS] if content else None
            break

        # Related active TaskFlows (LT-2 registry).
        taskflow_ids = _get_active_taskflow_ids_sync(session_id)

        save_session_end_state(
            session_id=session_id,
            channel_id=channel_id or "",
            chat_id=chat_id or "",
            summary=last_ai or "",
            taskflow_ids=taskflow_ids,
        )
        logger.debug("Session continuity saved for session={}", session_id)
    except Exception as e:
        logger.warning("Failed to save session end state: {}", e)


def _get_channel_chat_for_session(session_id: str) -> tuple[str, str]:
    """Resolve the ``(channel_id, chat_id)`` bound to a session.

    Looks the binding up in ``relation_register`` (channel sessions register it
    via ``register_channel_chat``). Falls back to ``("", "")`` when the session
    has no channel binding (pure websocket sessions) or on any failure.
    """
    try:
        from runtime.relation_register import relation_register

        binding = relation_register.get_channel_chat_id_by_session_id(session_id)
        if binding:
            channel_id, chat_id = binding
            return channel_id or "", chat_id or ""
    except Exception as e:
        logger.warning("Failed to resolve channel/chat for session {}: {}", session_id, e)
    return "", ""


def _get_active_taskflow_ids_sync(session_id: str) -> list[str]:
    """Collect the ids of active TaskFlows created by this session (LT-2).

    Uses the taskflow registry's synchronous read and filters by
    ``creator_session_key``. Returns ``[]`` on any failure (fail-open).
    """
    try:
        from agent.tools.taskflow.registry import store_sqlite
        from agent.tools.taskflow.tools._shared import requester_session_key

        creator_key = requester_session_key(session_id)
        active_flows = store_sqlite.get_active_flows_sync()
        mine = [
            flow
            for flow in active_flows
            if (flow.get("state") or {}).get("creator_session_key") == creator_key
        ]
        return [str(flow["flow_id"]) for flow in mine]
    except Exception as e:
        logger.warning("Failed to collect active taskflows for session {}: {}", session_id, e)
        return []


def _safe_filename(key: str) -> str:
    """Convert a ``channel:chat`` (or session) key into a filesystem-safe name."""
    return key.replace(":", "_").replace("/", "_").replace("\\", "_")
