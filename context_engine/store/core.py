import json
import sqlite3
import threading
from .db import get_db
from typing import Annotated, Any
from datetime import datetime
from pydantic import Field, validate_call
from langchain_core.messages import BaseMessage


# Shared SQLite connection instance used by all store operations in this module.
_db: sqlite3.Connection = get_db()

# Audit #5: serializes the read-MAX-then-INSERT turn assignment inside
# ``add_messages``. The store runs on a single shared connection in autocommit
# mode (``isolation_level=None``), so without this lock two concurrent
# ``add_messages`` calls on the same session could both observe the same
# ``MAX(turn_num)`` and silently merge two turns into one.
_turn_assign_lock = threading.Lock()


def get_max_turn_num(session_id: str) -> int:
    """Get the maximum turn_num recorded for a session.

    Returns 0 when the session has no messages yet.
    """
    max_turn_num_row = _db.execute(
        "SELECT MAX(turn_num) FROM messages WHERE session_id = ?", (session_id,)
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
    if messages is None or len(messages) == 0:
        return

    # A turn here is a group of messages. Each add_messages call starts a new
    # turn. Rows are built with a 0 placeholder below; the real turn number is
    # assigned atomically under _turn_assign_lock right before the INSERT
    # (audit #5) so a concurrent same-session writer can never share it.
    current_turn: int = 0

    # All messages in this batch share the same timestamp (YYYYMMDDHHmmss).
    base_timestamp: str = datetime.now().strftime("%Y%m%d%H%M%S")

    # Rows to be bulk-inserted by executemany.
    insert_rows: list[dict] = []

    # Normalize each LangChain message into a raw DB row according to its role.
    for m in messages:
        # AI message: keep content and optional tool_calls (JSON-encoded).
        if m.type == "ai":
            # Extract model + token usage metadata for frontend display. Both
            # are optional: some providers omit them, so every lookup is
            # guarded and defaults to None.
            response_metadata: dict[str, Any] = getattr(m, "response_metadata", None) or {}
            model_name: str | None = response_metadata.get("model_name") or response_metadata.get(
                "model"
            )
            usage_metadata: dict[str, Any] | None = getattr(m, "usage_metadata", None)
            input_tokens: int | None = None
            output_tokens: int | None = None
            if usage_metadata:
                if usage_metadata.get("input_tokens") is not None:
                    input_tokens = int(usage_metadata["input_tokens"])
                if usage_metadata.get("output_tokens") is not None:
                    output_tokens = int(usage_metadata["output_tokens"])

            # Persist the chain-of-thought so the client can re-render the
            # collapsible thinking bubble after a reload. Reasoning models
            # (DeepSeek thinking, GLM thinking, R1...) carry the complete CoT
            # on the final aggregated message under
            # additional_kwargs["reasoning_content"] (the reasoning normalizer
            # emits per-chunk deltas, which langchain's chunk aggregation
            # concatenates back into the full text). The client history
            # mapping reads the `reasoning` column.
            ai_additional_kwargs: dict[str, Any] = getattr(m, "additional_kwargs", None) or {}
            reasoning_text: str | None = ai_additional_kwargs.get("reasoning_content") or None

            insert_rows.append(
                {
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
                    "reasoning": reasoning_text,
                    "reasoning_content": None,
                    "images": None,
                    "audios": None,
                    "videos": None,
                    "model_name": model_name,
                    "input_tokens": input_tokens,
                    "output_tokens": output_tokens,
                    "origin": None,
                }
            )
        elif m.type == "human":
            additional_kwargs: dict[str, str] = getattr(m, "additional_kwargs", {})

            # Filter out human messages produced by summarization,
            # so compressed history doesn't pollute the raw store.
            if additional_kwargs.get("lc_source", None) == "summarization":
                continue

            # Persist any media file paths declared by the multimodal processor.
            images: list[str] = additional_kwargs.get("images", []) or []
            audios: list[str] = additional_kwargs.get("audios", []) or []
            videos: list[str] = additional_kwargs.get("videos", []) or []

            # Tag background subagent-completion injections. The tag fires
            # ONLY on a full match of the frozen metadata contract built by
            # agent/tools/subagent/announce/completion_message.py (mirrors
            # _is_internal_completion in the completion-drain middleware):
            # internal must be True (strict bool, not merely truthy) AND
            # provenance must be exactly "subagent_completion". Everything
            # else — plain user input, partial-contract metadata — stays
            # NULL (= real user message). Never an empty string.
            meta: dict[str, Any] = getattr(m, "metadata", None) or {}
            origin: str | None = (
                "subagent_completion"
                if (
                    meta.get("internal") is True
                    and meta.get("provenance") == "subagent_completion"
                )
                else None
            )

            insert_rows.append(
                {
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
                    "model_name": None,
                    "input_tokens": None,
                    "output_tokens": None,
                    "origin": origin,
                }
            )
        elif m.type == "tool":
            # Tool message: carry tool metadata (call id, name, execution status).
            insert_rows.append(
                {
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
                    "model_name": None,
                    "input_tokens": None,
                    "output_tokens": None,
                    "origin": None,
                }
            )

    # Audit #5: assign the turn number atomically. Re-read MAX(turn_num) and
    # insert while holding the module-level lock, so two concurrent writers on
    # the same session can never observe the same MAX and silently merge two
    # turns into one. No explicit BEGIN: the connection is in autocommit mode
    # (isolation_level=None) and a concurrent reader's ``with _db:`` exit would
    # commit an open transaction early — the lock is what serializes writers.
    with _turn_assign_lock:
        current_turn = get_max_turn_num(session_id) + 1
        for row in insert_rows:
            row["turn_num"] = current_turn

        # Bulk-insert all accumulated rows of this turn (autocommit: each row
        # commits on execution).
        _db.executemany(
            """
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
                videos,
                model_name,
                input_tokens,
                output_tokens,
                origin
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
                :videos,
                :model_name,
                :input_tokens,
                :output_tokens,
                :origin
            )
        """,
            insert_rows,
        )

    # No-op in autocommit mode; kept so the batch also commits as one unit if
    # the connection ever switches to implicit-transaction mode.
    _db.commit()


def get_turns_by_turn_num_scope(
    session_id: str, target_turn_num: int, half_scope: int = 5
) -> list[dict]:
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

        rows = _db.execute(
            """
            SELECT * FROM messages 
            WHERE session_id = ? AND turn_num >= ? AND turn_num <= ?
            ORDER BY turn_num DESC, id ASC
        """,
            (session_id, min_turn_num, max_turn_num),
        ).fetchall()

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

        rows = _db.execute(
            """
            select * from messages
            where session_id = ? and turn_num >= ? and turn_num <= ?
            ORDER BY turn_num DESC, id ASC
        """,
            (session_id, target_start_turn_num, target_end_turn_num),
        ).fetchall()

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
    return get_history_by_turn_page(
        session_id, min_turn_num=1, turn_page_size=last_n, turn_page_num=1
    )


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
        cur = _db.execute("DELETE FROM messages WHERE session_id = ?", (session_id,))
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
            if (
                isinstance(part, dict)
                and part.get("type") == "text"
                and isinstance(part.get("text"), str)
            ):
                return part["text"].strip()
    return ""


def _is_top_level_session(session_id: str) -> bool:
    """Return True for a top-level (user-facing) session.

    Subagent sessions are namespaced under a reserved
    ``agent:<agent_id>:subagent:`` prefix hierarchy and must NOT be surfaced in
    the user's session list. Every key in that hierarchy (top-level child,
    grandchild, etc.) carries the ``:subagent:`` segment, e.g.:
    - ``agent:main:subagent:<uuid>``                  — a top-level child agent.
    - ``agent:main:subagent:<uuid>:subagent:<uuid>``  — a nested grandchild.
    See ``agent/tools/subagent/spawn/core.py::child_session_key`` for
    construction.
    """
    return ":subagent:" not in session_id


def get_session_ids() -> list[dict]:
    """Enumerate all distinct top-level sessions from the messages table.

    Returns one row per session, ordered by most recent activity first:

        [{
            "session_id": str,
            "last_time":   str,   # newest message timestamp (YYYYMMDDHHmmss)
            "title":       str,   # derived from the latest human message; "" when no usable text
        }, ...]

    Subagent sessions (keyed with an ``agent:<agent_id>:subagent:`` prefix
    hierarchy) are excluded, so only user-facing conversations are listed.

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
        # (the user's most recent question). Background subagent-completion
        # injections (``origin = 'subagent_completion'``) are excluded, so a
        # carrier never becomes the session title; a session whose only human
        # rows are carriers gets an empty title, which the client renders as
        # an i18n placeholder (e.g. "新会话") instead of leaking the raw
        # session_id.
        title: str = ""
        with _db:
            last_msg = _db.execute(
                """
                SELECT content FROM messages
                WHERE session_id = ? AND role = 'human' AND origin IS NULL
                ORDER BY turn_num DESC, id DESC
                LIMIT 1
            """,
                (session_id,),
            ).fetchone()
        if last_msg is not None:
            title = _decode_title_content(
                last_msg["content"] if last_msg["content"] is not None else None
            )
        result.append(
            {
                "session_id": session_id,
                "last_time": str(row["last_time"]),
                "title": title,
            }
        )
    return result
