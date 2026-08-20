import json
import sqlite3
from .db import get_db
from typing import Annotated
from datetime import datetime
from pydantic import Field, validate_call
from langchain_core.messages import BaseMessage


# Shared SQLite connection instance used by all store operations in this module.
_db:sqlite3.Connection = get_db()

def get_max_turn_num(session_id: str) -> int:
    """Get the maximum turn_num recorded for a session.

    Returns 0 when the session has no messages yet.
    """
    max_turn_num_row = _db.execute(
        "SELECT MAX(turn_num) FROM messages WHERE session_id = ?",
        (session_id,)
    ).fetchone()
    return max_turn_num_row[0] if max_turn_num_row and max_turn_num_row[0] is not None else 0

async def add_messages(session_id: str, messages: list[BaseMessage]) -> None:
    """Persist a batch of LangChain messages as a new turn in the messages table.

    All messages passed in one call share the same (auto-incremented) turn_num
    and a single timestamp derived from the current time.

    Args:
        session_id: The session these messages belong to.
        messages: The LangChain BaseMessage list (human / ai / tool roles).
    """
    # Early exit when there is nothing to persist.
    if messages is None or len(messages)==0:
        return

    # A turn here is a group of messages. Each add_messages call starts a new turn.
    current_turn: int = get_max_turn_num(session_id)
    current_turn+=1

    # All messages in this batch share the same timestamp (YYYYMMDDHHmmss).
    base_timestamp: str = datetime.now().strftime("%Y%m%d%H%M%S")

    # Rows to be bulk-inserted by executemany.
    insert_rows: list[dict] = []

    # Normalize each LangChain message into a raw DB row according to its role.
    for m in messages:
        # AI message: keep content and optional tool_calls (JSON-encoded).
        if m.type == "ai":
            insert_rows.append({
                "session_id": session_id,
                "turn_num": current_turn,
                "role": m.type,
                "content": json.dumps(getattr(m, "content", ""), ensure_ascii=False),
                "tool_call_id": None,
                "tool_calls": json.dumps(getattr(m, "tool_calls", None), ensure_ascii=False),
                "tool_status": None,
                "tool_name": None,
                "timestamp": base_timestamp,
                "finish_reason": None,
                "reasoning": None,
                "reasoning_content": None,
                "images": None,
                "audios": None,
                "videos": None,
            })
        elif m.type == "human":
            additional_kwargs:dict[str, str] = getattr(m, "additional_kwargs", {})

            # Filter out human messages produced by summarization,
            # so compressed history doesn't pollute the raw store.
            if additional_kwargs.get("lc_source", None) == "summarization":
                continue

            # Persist any media file paths declared by the multimodal processor.
            images: list[str] = additional_kwargs.get("images", []) or []
            audios: list[str] = additional_kwargs.get("audios", []) or []
            videos: list[str] = additional_kwargs.get("videos", []) or []

            insert_rows.append({
                "session_id": session_id,
                "turn_num": current_turn,
                "role": m.type,
                "content": json.dumps(getattr(m, "content", ""), ensure_ascii=False),
                "tool_call_id": None,
                "tool_calls": None,
                "tool_status": None,
                "tool_name": None,
                "timestamp": base_timestamp,
                "finish_reason": None,
                "reasoning": None,
                "reasoning_content": None,
                "images": json.dumps(images, ensure_ascii=False) if images else None,
                "audios": json.dumps(audios, ensure_ascii=False) if audios else None,
                "videos": json.dumps(videos, ensure_ascii=False) if videos else None,
            })
        elif m.type == "tool":
            # Tool message: carry tool metadata (call id, name, execution status).
            insert_rows.append({
                "session_id": session_id,
                "turn_num": current_turn,
                "role": m.type,
                "content": json.dumps(getattr(m, "content", ""), ensure_ascii=False),
                "tool_call_id": getattr(m, "tool_call_id", None),
                "tool_calls": None,
                "tool_name": getattr(m, "name", None),
                "tool_status": getattr(m, "status", "success"),
                "finish_reason": None,
                "reasoning": None,
                "reasoning_content": None,
                "timestamp": base_timestamp,
                "images": None,
                "audios": None,
                "videos": None,
            })

    # Bulk-insert all accumulated rows in a single transaction.
    _db.executemany("""
        INSERT INTO messages (
            session_id,
            turn_num,
            role,
            content,
            tool_call_id,
            tool_calls,
            tool_status,
            tool_name,
            timestamp,
            finish_reason,
            reasoning,
            reasoning_content,
            images,
            audios,
            videos
        ) VALUES (
            :session_id,
            :turn_num,
            :role,
            :content,
            :tool_call_id,
            :tool_calls,
            :tool_status,
            :tool_name,
            :timestamp,
            :finish_reason,
            :reasoning,
            :reasoning_content,
            :images,
            :audios,
            :videos
        )
    """, insert_rows)

    # Persist the batch atomically.
    _db.commit()

def get_turns_by_turn_num_scope(session_id: str, target_turn_num: int, half_scope: int = 5) -> list[dict]:
    """Fetch messages whose turn_num falls within a range centered on a target turn.

    Args:
        session_id: The session to query.
        target_turn_num: The central turn number.
        half_scope: How many turns to include on each side of the target (default: 5).

    Returns:
        A list of message row dicts, newest turn first, with JSON columns decoded.
    """
    with _db:
        max_turn_num: int = get_max_turn_num(session_id)
        min_turn_num: int = 1

        # Return empty when the session has no messages at all.
        if max_turn_num == 0:
            return []

        # Clamp the requested window to the actual data range.
        max_turn_num = min(max_turn_num, target_turn_num + half_scope)
        min_turn_num = max(min_turn_num, target_turn_num - half_scope)

        rows = _db.execute(f"""
            SELECT * FROM messages 
            WHERE session_id = ? AND turn_num >= ? AND turn_num <= ?
            ORDER BY turn_num DESC, id ASC
        """, (session_id, min_turn_num, max_turn_num)).fetchall()

        if rows is None or len(rows) == 0:
            return []

        # Decode JSON-encoded content and tool_calls back into Python objects.
        result: list[dict] = []
        for row in rows:
            row = dict(row)
            if isinstance(row["content"], str):
                row["content"] = json.loads(row["content"])
            if isinstance(row["tool_calls"], str):
                row["tool_calls"] = json.loads(row["tool_calls"])
            if isinstance(row["images"], str):
                row["images"] = json.loads(row["images"])
            if isinstance(row["audios"], str):
                row["audios"] = json.loads(row["audios"])
            if isinstance(row["videos"], str):
                row["videos"] = json.loads(row["videos"])
            result.append(row)

        return result

@validate_call
def get_history_by_turn_page(
    session_id: str,
    min_turn_num: Annotated[int, Field(ge=1)] = 1,
    turn_page_size: Annotated[int, Field(ge=1)] = 10,
    turn_page_num: Annotated[int, Field(ge=1)] = 1,
) -> list[dict]:
    """Fetch a page of message history, paginated by turn number.

    Pages are ordered from newest to oldest: page 1 covers the most recent
    turn_page_size turns. A lower bound can be given via min_turn_num so the
    page never dips below it.

    Args:
        session_id: The session to query.
        min_turn_num: Inclusive lower bound for turn_num (>= 1).
        turn_page_size: Turns per page (>= 1).
        turn_page_num: 1-based page index from the newest turn backward.

    Returns:
        A list of message row dicts, newest turn first, with JSON columns decoded.
    """
    with _db:
        max_turn_num: int = get_max_turn_num(session_id)

        # Short circuit when there is no history for this session.
        if max_turn_num == 0:
            return []

        # Compute the turn window for the requested page.
        target_end_turn_num: int = max_turn_num - (turn_page_num - 1) * turn_page_size

        target_start_turn_num: int = target_end_turn_num - turn_page_size + 1

        # Never page below the requested lower bound.
        if target_start_turn_num < min_turn_num:
            target_start_turn_num = min_turn_num

        rows = _db.execute("""
            select * from messages
            where session_id = ? and turn_num >= ? and turn_num <= ?
            ORDER BY turn_num DESC, id ASC
        """, (session_id, target_start_turn_num, target_end_turn_num)).fetchall()

        if rows is None or len(rows) == 0:
            return []

        # Decode JSON-encoded content and tool_calls back into Python objects.
        result: list[dict] = []
        for row in rows:
            row = dict(row)
            if isinstance(row["content"], str):
                row["content"] = json.loads(row["content"])
            if isinstance(row["tool_calls"], str):
                row["tool_calls"] = json.loads(row["tool_calls"])
            if isinstance(row["images"], str):
                row["images"] = json.loads(row["images"])
            if isinstance(row["audios"], str):
                row["audios"] = json.loads(row["audios"])
            if isinstance(row["videos"], str):
                row["videos"] = json.loads(row["videos"])
            result.append(row)

        return result


def get_messages_by_lastest_n_turns(session_id: str, last_n: int = 5) -> list[dict]:
    """Convenience wrapper: fetch the last `last_n` turns of history.

    Delegates to paginated history with page 1 and the desired page size.
    """
    return get_history_by_turn_page(session_id, min_turn_num=1, turn_page_size=last_n, turn_page_num=1)


def delete_messages_by_session(session_id: str) -> int:
    """Delete all messages belonging to a session from the SQLite store.

    The FTS5 triggers on the ``messages`` table (see ``store/db.py``) purge the
    matching rows from both FTS indexes automatically, so no FTS cleanup is
    needed here.

    Args:
        session_id: The session whose message rows should be removed.

    Returns:
        The number of rows deleted.
    """
    with _db:
        cur = _db.execute(
            "DELETE FROM messages WHERE session_id = ?",
            (session_id,)
        )
    return cur.rowcount


def _decode_title_content(raw_content: str | None) -> str:
    """Decode a stored content cell into plain text suitable as a session title.

    Stored content is ``json.dumps(...)`` — a JSON-encoded string. It may be a
    plain text string, or a multimodal structured array like
    ``[{"type":"text","text":"..."},{"type":"image",...}]``. This extractor
    returns the first text segment (trimmed), or a fallback when nothing usable.
    """
    try:
        decoded = json.loads(raw_content) if raw_content else None
    except (json.JSONDecodeError, TypeError):
        decoded = raw_content
    if isinstance(decoded, str):
        return decoded.strip()
    if isinstance(decoded, list):
        for part in decoded:
            if isinstance(part, dict) and part.get("type") == "text" and isinstance(part.get("text"), str):
                return part["text"].strip()
    return ""


def _is_top_level_session(session_id: str) -> bool:
    """Return True for a top-level (user-facing) session.

    Subagent sessions carry a reserved prefix and must NOT be surfaced in the
    user's session list:
    - ``commander-<master_session_id>`` — the hierarchy Commander agent.
    - ``worker-<commander_session_id>-<task_id>`` — a task Worker agent.
    See ``agent/tools/subagent/base.py`` and
    ``agent/tools/subagent/commander/tools/worker/core.py`` for construction.
    """
    return not (session_id.startswith("commander-") or session_id.startswith("worker-"))


def get_session_ids() -> list[dict]:
    """Enumerate all distinct top-level sessions from the messages table.

    Returns one row per session, ordered by most recent activity first:

        [{
            "session_id": str,
            "last_time":   str,   # newest message timestamp (YYYYMMDDHHmmss)
            "title":       str,   # derived from the latest human message; "" when no usable text
        }, ...]

    Subagent sessions (``commander-*`` / ``worker-*``) are excluded, so only
    user-facing conversations are listed.

    ``last_time`` is the newest message's ``timestamp`` text (the same
    ``YYYYMMDDHHmmss`` format used across the store).
    """
    with _db:
        rows = _db.execute("""
            SELECT m.session_id, m.last_time
            FROM (
                SELECT session_id, MAX(timestamp) AS last_time
                FROM messages
                GROUP BY session_id
            ) m
            ORDER BY m.last_time DESC
        """).fetchall()

    result: list[dict] = []
    for row in rows:
        session_id = str(row["session_id"])
        # Skip subagent (commander/worker) sessions.
        if not _is_top_level_session(session_id):
            continue
        # Derive a title from the latest human message of the session
        # (the user's most recent question).
        # An empty title means that human message had no usable text; the
        # client renders an i18n placeholder (e.g. "新会话") in that case
        # instead of leaking the raw session_id.
        title: str = ""
        with _db:
            last_msg = _db.execute("""
                SELECT content FROM messages
                WHERE session_id = ? AND role = 'human'
                ORDER BY turn_num DESC, id DESC
                LIMIT 1
            """, (session_id,)).fetchone()
        if last_msg is not None:
            title = _decode_title_content(last_msg["content"] if last_msg["content"] is not None else None)
        result.append({
            "session_id": session_id,
            "last_time": str(row["last_time"]),
            "title": title,
        })
    return result