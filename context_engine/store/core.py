import hashlib
import json
import sqlite3
import threading
from .db import get_db
from abc import ABC, abstractmethod
from typing import Annotated, Any
from datetime import datetime, timedelta
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

# Audit #21: strictly-increasing turn stamps. Two turns in the same second
# (or even the same millisecond) must never share a stamp, or session
# ordering (MAX(ts_ms)) ties. Same-ms calls are bumped 1ms apart.
_turn_stamp_lock = threading.Lock()
_last_turn_ms: int | None = None


def _as_str_list(value: Any) -> list[str]:
    return [str(p) for p in value] if isinstance(value, list) else []


def _next_turn_stamp() -> tuple[int, str]:
    """Return (epoch-ms, 14-char display stamp) for a new turn.

    The epoch-ms value is strictly increasing across calls within this
    process: same-millisecond calls are bumped 1ms apart so session ordering
    (MAX(ts_ms)) can never tie.
    """
    global _last_turn_ms
    with _turn_stamp_lock:
        now = datetime.now()
        ms = int(now.timestamp() * 1000)
        if _last_turn_ms is not None and ms <= _last_turn_ms:
            now = datetime.fromtimestamp(_last_turn_ms / 1000) + timedelta(milliseconds=1)
            ms = int(now.timestamp() * 1000)
        _last_turn_ms = ms
    return ms, now.strftime("%Y%m%d%H%M%S")


def get_max_turn_num(session_id: str) -> int:
    """Get the maximum turn_num recorded for a session.

    Returns 0 when the session has no messages yet.
    """
    max_turn_num_row = _db.execute(
        "SELECT MAX(turn_num) FROM messages WHERE session_id = ?", (session_id,)
    ).fetchone()
    return max_turn_num_row[0] if max_turn_num_row and max_turn_num_row[0] is not None else 0


class MessageRowBuilder(ABC):
    @abstractmethod
    def build(self, msg: BaseMessage, session_id: str) -> dict | None: ...


class AIMessageRowBuilder(MessageRowBuilder):
    def build(self, msg: BaseMessage, session_id: str) -> dict:
        # Extract model + token usage metadata for frontend display. Both
        # are optional: some providers omit them, so every lookup is
        # guarded and defaults to None.
        response_metadata: dict[str, Any] = getattr(msg, "response_metadata", None) or {}
        model_name: str | None = response_metadata.get("model_name") or response_metadata.get(
            "model"
        )
        usage_metadata: dict[str, Any] | None = getattr(msg, "usage_metadata", None)
        input_tokens: int | None = None
        output_tokens: int | None = None
        reasoning_tokens: int | None = None
        if usage_metadata:
            if usage_metadata.get("input_tokens") is not None:
                input_tokens = int(usage_metadata["input_tokens"])
            if usage_metadata.get("output_tokens") is not None:
                output_tokens = int(usage_metadata["output_tokens"])
            _details = usage_metadata.get("output_token_details") or {}
            if _details.get("reasoning_tokens") is not None:
                reasoning_tokens = int(_details["reasoning_tokens"])

        # Persist the chain-of-thought so the client can re-render the
        # collapsible thinking bubble after a reload. Reasoning models
        # (DeepSeek thinking, GLM thinking, R1...) carry the complete CoT
        # on the final aggregated message under
        # additional_kwargs["reasoning_content"] (the reasoning normalizer
        # emits per-chunk deltas, which langchain's chunk aggregation
        # concatenates back into the full text). The client history
        # mapping reads the `reasoning` column.
        ai_additional_kwargs: dict[str, Any] = getattr(msg, "additional_kwargs", None) or {}
        reasoning_text: str | None = ai_additional_kwargs.get("reasoning_content") or None
        finish_reason: str | None = response_metadata.get("finish_reason") or (
            response_metadata.get("stop_reason")
        )

        return {
            "session_id": session_id,
            "turn_num": 0,
            "role": msg.type,
            "content": json.dumps(getattr(msg, "content", ""), ensure_ascii=False),
            "tool_call_id": None,
            "tool_calls": json.dumps(getattr(msg, "tool_calls", None), ensure_ascii=False),
            "tool_status": None,
            "tool_name": None,
            "timestamp": "",
            "ts_ms": 0,
            "finish_reason": finish_reason,
            "reasoning": reasoning_text,
            "reasoning_content": None,
            "images": None,
            "audios": None,
            "videos": None,
            "model_name": model_name,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "reasoning_tokens": reasoning_tokens,
            "origin": None,
        }


class HumanMessageRowBuilder(MessageRowBuilder):
    def build(self, msg: BaseMessage, session_id: str) -> dict | None:
        additional_kwargs: dict[str, str] = getattr(msg, "additional_kwargs", {})

        # Filter out human messages produced by summarization,
        # so compressed history doesn't pollute the raw store.
        if additional_kwargs.get("lc_source", None) == "summarization":
            return None

        # Persist any media file paths declared by the multimodal processor.
        images: list[str] = _as_str_list(additional_kwargs.get("images", []))
        audios: list[str] = _as_str_list(additional_kwargs.get("audios", []))
        videos: list[str] = _as_str_list(additional_kwargs.get("videos", []))

        # Tag background subagent-completion injections. The tag fires
        # ONLY on a full match of the frozen metadata contract built by
        # agent/tools/subagent/announce/completion_message.py (mirrors
        # _is_internal_completion in the completion-drain middleware):
        # internal must be True (strict bool, not merely truthy) AND
        # provenance must be exactly "subagent_completion". Everything
        # else — plain user input, partial-contract metadata — stays
        # NULL (= real user message). Never an empty string.
        meta: dict[str, Any] = getattr(msg, "metadata", None) or {}
        origin: str | None = (
            "subagent_completion"
            if (meta.get("internal") is True and meta.get("provenance") == "subagent_completion")
            else None
        )

        return {
            "session_id": session_id,
            "turn_num": 0,
            "role": msg.type,
            "content": json.dumps(getattr(msg, "content", ""), ensure_ascii=False),
            "tool_call_id": None,
            "tool_calls": None,
            "tool_status": None,
            "tool_name": None,
            "timestamp": "",
            "ts_ms": 0,
            "finish_reason": None,
            "reasoning": None,
            "reasoning_content": None,
            "images": json.dumps(images, ensure_ascii=False) if images else None,
            "audios": json.dumps(audios, ensure_ascii=False) if audios else None,
            "videos": json.dumps(videos, ensure_ascii=False) if videos else None,
            "model_name": None,
            "input_tokens": None,
            "output_tokens": None,
            "reasoning_tokens": None,
            "origin": origin,
        }


class ToolMessageRowBuilder(MessageRowBuilder):
    def build(self, msg: BaseMessage, session_id: str) -> dict:
        # Tool message: carry tool metadata (call id, name, execution status).
        return {
            "session_id": session_id,
            "turn_num": 0,
            "role": msg.type,
            "content": json.dumps(getattr(msg, "content", ""), ensure_ascii=False),
            "tool_call_id": getattr(msg, "tool_call_id", None),
            "tool_calls": None,
            "tool_name": getattr(msg, "name", None),
            "tool_status": getattr(msg, "status", "success"),
            "finish_reason": None,
            "reasoning": None,
            "reasoning_content": None,
            "timestamp": "",
            "ts_ms": 0,
            "images": None,
            "audios": None,
            "videos": None,
            "model_name": None,
            "input_tokens": None,
            "output_tokens": None,
            "reasoning_tokens": None,
            "origin": None,
        }


# SESSION plan P1-2: crash-retry idempotency for message persistence. Messages
# already flushed in this process carry "_db_persisted" in additional_kwargs
# and are skipped on retry; the per-row idempotency key deduplicates at the
# storage level (partial unique index) for cross-process replay safety.
_DB_PERSISTED_KEY = "_db_persisted"

_BUILDERS: dict[str, MessageRowBuilder] = {
    "ai": AIMessageRowBuilder(),
    "human": HumanMessageRowBuilder(),
    "tool": ToolMessageRowBuilder(),
}


def get_session_leaf(session_id: str) -> int | None:
    """Current tree leaf of a session (SESSION plan P1-5); None = no tree yet."""
    row = _db.execute(
        "SELECT leaf_message_id FROM session_leafs WHERE session_id = ?", (session_id,)
    ).fetchone()
    return row[0] if row else None


def set_session_leaf(session_id: str, leaf_message_id: int) -> None:
    _db.execute(
        "INSERT INTO session_leafs (session_id, leaf_message_id, updated_at) "
        "VALUES (?, ?, ?) ON CONFLICT(session_id) DO UPDATE SET "
        "leaf_message_id = excluded.leaf_message_id, updated_at = excluded.updated_at",
        (session_id, leaf_message_id, datetime.now().strftime("%Y%m%d%H%M%S")),
    )


def get_message_by_id(session_id: str, message_id: int) -> dict | None:
    # Lookup by id only: ids are globally unique (AUTOINCREMENT PK) and a fork
    # intentionally crosses session boundaries (shared tree nodes).
    row = _db.execute("SELECT * FROM messages WHERE id = ?", (message_id,)).fetchone()
    return _decode_json_columns(dict(row)) if row else None


def get_message_path_to_root(session_id: str, leaf_message_id: int) -> list[dict]:
    """Walk the message tree from a leaf back to its root, oldest first."""
    path: list[dict] = []
    current_id: int | None = leaf_message_id
    seen: set[int] = set()
    while current_id is not None and current_id not in seen:
        seen.add(current_id)
        msg = get_message_by_id(session_id, current_id)
        if not msg:
            break
        path.append(msg)
        current_id = msg.get("parent_message_id")
    path.reverse()
    return path


def fork_from_message(source_session_id: str, target_message_id: int, new_session_id: str) -> None:
    """Fork at a node: the new session's leaf points at the source message —
    no message copying, the tree IS the history."""
    set_session_leaf(new_session_id, target_message_id)


def _idempotency_key(session_id: str, turn_num: int, ts_ms: int, index: int, m: BaseMessage) -> str:
    """Stable per-row key for INSERT OR IGNORE dedup (SESSION plan P1-2)."""
    payload = json.dumps(
        {
            "role": m.type,
            "content": m.content,
            "tool_call_id": getattr(m, "tool_call_id", None),
        },
        ensure_ascii=False,
        sort_keys=True,
        default=str,
    )
    digest = hashlib.sha1(payload.encode("utf-8")).hexdigest()[:16]
    return f"{session_id}:{turn_num}:{ts_ms}:{index}:{digest}"


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

    # SESSION plan P1-2: skip messages this process already flushed (crash-retry dedup).
    pending: list[BaseMessage] = [
        m for m in messages if not m.additional_kwargs.get(_DB_PERSISTED_KEY)
    ]
    if not pending:
        return

    # Rows to be bulk-inserted by executemany (paired with their source message
    # so the idempotency key can be computed after the turn stamp is assigned).
    insert_rows: list[dict] = []
    paired: list[tuple[BaseMessage, dict]] = []
    for m in pending:
        builder = _BUILDERS.get(m.type)
        if builder is None:
            continue
        row = builder.build(m, session_id)
        if row is not None:
            insert_rows.append(row)
            paired.append((m, row))

    # Audit #5: assign the turn number atomically. Re-read MAX(turn_num) and
    # insert while holding the module-level lock, so two concurrent writers on
    # the same session can never observe the same MAX and silently merge two
    # turns into one. No explicit BEGIN: the connection is in autocommit mode
    # (isolation_level=None) and a concurrent reader's ``with _db:`` exit would
    # commit an open transaction early — the lock is what serializes writers.
    with _turn_assign_lock:
        # Stamp here, not before row building (history-jumble race): stamping
        # inside the same critical section as the turn assignment makes
        # (stamp order == turn order) atomic, so a concurrent writer can never
        # persist turn N+1 with an earlier ts_ms than turn N.
        turn_ms, base_timestamp = _next_turn_stamp()
        current_turn = get_max_turn_num(session_id) + 1
        for index, (message, row) in enumerate(paired):
            row["turn_num"] = current_turn
            row["timestamp"] = base_timestamp
            row["ts_ms"] = turn_ms
            row["idempotency_key"] = _idempotency_key(
                session_id, current_turn, turn_ms, index, message
            )
            # SESSION plan P1-3: emitters mark ineligible messages via
            # additional_kwargs["context_eligible"] = False (default eligible).
            row["context_eligible"] = (
                0 if message.additional_kwargs.get("context_eligible") is False else 1
            )

        # SESSION plan P1-5: chain the batch into the message tree. The first
        # row parents to the session's current leaf (or NULL when forking from
        # nothing); each following row chains to the previous one. Inserted
        # row-by-row (turn-sized batches) so every lastrowid is available.
        parent_id: int | None = get_session_leaf(session_id)
        for row in insert_rows:
            row["parent_message_id"] = parent_id
            cursor = _db.execute(
                """
                INSERT OR IGNORE INTO messages (
                    session_id,
                    turn_num,
                    role,
                    content,
                    tool_call_id,
                    tool_calls,
                    tool_status,
                    tool_name,
                    timestamp,
                    ts_ms,
                    finish_reason,
                    reasoning,
                    reasoning_content,
                    images,
                    audios,
                    videos,
                    model_name,
                    input_tokens,
                    output_tokens,
                    reasoning_tokens,
                    origin,
                    idempotency_key,
                    context_eligible,
                    parent_message_id
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
                    :ts_ms,
                    :finish_reason,
                    :reasoning,
                    :reasoning_content,
                    :images,
                    :audios,
                    :videos,
                    :model_name,
                    :input_tokens,
                    :output_tokens,
                    :reasoning_tokens,
                    :origin,
                    :idempotency_key,
                    :context_eligible,
                    :parent_message_id
                )
                """,
                row,
            )
            if cursor.lastrowid:
                parent_id = cursor.lastrowid

        if parent_id is not None:
            set_session_leaf(session_id, parent_id)

    # SESSION plan P1-2: mark the batch as flushed so an in-process retry of
    # the same message list is skipped instead of double-written.
    for message in pending:
        message.additional_kwargs[_DB_PERSISTED_KEY] = True

    # No-op in autocommit mode; kept so the batch also commits as one unit if
    # the connection ever switches to implicit-transaction mode.
    _db.commit()


def create_compaction_checkpoint(
    session_id: str,
    pre_compaction_turn: int,
    post_compaction_turn: int,
    summary_text: str = "",
) -> int:
    """Record a checkpoint after a successful compaction (SESSION plan P1-1)."""
    seq_row = _db.execute(
        "SELECT COALESCE(MAX(checkpoint_seq), -1) + 1 FROM compaction_checkpoints "
        "WHERE session_id = ?",
        (session_id,),
    ).fetchone()
    seq = seq_row[0]
    created = datetime.now().strftime("%Y%m%d%H%M%S")
    cursor = _db.execute(
        "INSERT INTO compaction_checkpoints (session_id, checkpoint_seq, "
        "pre_compaction_turn, post_compaction_turn, summary_text, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (session_id, seq, pre_compaction_turn, post_compaction_turn, summary_text, created),
    )
    return int(cursor.lastrowid or 0)


def get_compaction_checkpoint(session_id: str, checkpoint_id: int) -> dict | None:
    row = _db.execute(
        "SELECT * FROM compaction_checkpoints WHERE session_id = ? AND id = ?",
        (session_id, checkpoint_id),
    ).fetchone()
    return dict(row) if row else None


def mark_messages_compacted(
    session_id: str,
    up_to_turn: int | None = None,
    from_turn: int | None = None,
    checkpoint_id: int | None = None,
) -> int:
    """Soft-delete messages by turn range (kept on disk, excluded from context)."""
    sql = "UPDATE messages SET compacted = 1, compaction_checkpoint_id = ? WHERE session_id = ?"
    params: list[Any] = [checkpoint_id, session_id]
    if up_to_turn is not None:
        sql += " AND turn_num <= ?"
        params.append(up_to_turn)
    if from_turn is not None:
        sql += " AND turn_num >= ?"
        params.append(from_turn)
    cursor = _db.execute(sql, params)
    return cursor.rowcount


def unmark_messages_compacted(session_id: str, up_to_turn: int) -> int:
    cursor = _db.execute(
        "UPDATE messages SET compacted = 0, compaction_checkpoint_id = NULL "
        "WHERE session_id = ? AND turn_num <= ?",
        (session_id, up_to_turn),
    )
    return cursor.rowcount


def restore_compaction_checkpoint(session_id: str, checkpoint_id: int) -> dict:
    """Restore the context to a checkpoint: compact everything after it, unmark
    everything up to it. Returns the checkpoint for the caller."""
    checkpoint = get_compaction_checkpoint(session_id, checkpoint_id)
    if checkpoint is None:
        raise ValueError(f"checkpoint not found: {checkpoint_id}")
    _db.execute(
        "UPDATE messages SET compacted = 1, compaction_checkpoint_id = ? "
        "WHERE session_id = ? AND turn_num > ?",
        (checkpoint_id, session_id, checkpoint["pre_compaction_turn"]),
    )
    unmark_messages_compacted(session_id, up_to_turn=checkpoint["pre_compaction_turn"])
    return checkpoint


def _decode_json_columns(row: dict) -> dict:
    """Decode a message row's JSON-encoded cells in place and return it (audit 3.1.5).

    Shared by :func:`get_turns_by_turn_num_scope` and
    :func:`get_history_by_turn_page`: decodes the ``content``/``tool_calls``/
    ``images``/``audios``/``videos`` columns back into Python objects and pops
    the internal ordering column ``ts_ms``.
    """
    # Internal columns — not part of the client-facing shape.
    row.pop("ts_ms", None)
    row.pop("idempotency_key", None)
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
    return row


def get_turns_by_turn_num_scope(
    session_id: str,
    target_turn_num: int,
    half_scope: int = 5,
    only_eligible: bool = True,
    include_compacted: bool = False,
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

        compacted_filter = "" if include_compacted else " AND compacted = 0"
        eligible_filter = " AND context_eligible = 1" if only_eligible else ""
        rows = _db.execute(
            f"""
            SELECT * FROM messages 
            WHERE session_id = ? AND turn_num >= ? AND turn_num <= ?{compacted_filter}{eligible_filter}
            ORDER BY turn_num DESC, id ASC
        """,
            (session_id, min_turn_num, max_turn_num),
        ).fetchall()

        if rows is None or len(rows) == 0:
            return []

        # Decode JSON-encoded content and tool_calls back into Python objects.
        result: list[dict] = [_decode_json_columns(dict(row)) for row in rows]

        return result


@validate_call
def get_history_by_turn_page(
    session_id: str,
    min_turn_num: Annotated[int, Field(ge=1)] = 1,
    turn_page_size: Annotated[int, Field(ge=1)] = 10,
    turn_page_num: Annotated[int, Field(ge=1)] = 1,
    only_eligible: bool = True,
    include_compacted: bool = False,
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

        eligible_filter = " and context_eligible = 1" if only_eligible else ""
        compacted_filter = "" if include_compacted else " and compacted = 0"
        rows = _db.execute(
            f"""
            select * from messages
            where session_id = ? and turn_num >= ? and turn_num <= ?{eligible_filter}{compacted_filter}
            ORDER BY turn_num DESC, id ASC
        """,
            (session_id, target_start_turn_num, target_end_turn_num),
        ).fetchall()

        if rows is None or len(rows) == 0:
            return []

        # Decode JSON-encoded content and tool_calls back into Python objects.
        result: list[dict] = [_decode_json_columns(dict(row)) for row in rows]

        return result


def get_messages_by_lastest_n_turns(
    session_id: str, last_n: int = 5, only_eligible: bool = True
) -> list[dict]:
    """Convenience wrapper: fetch the last `last_n` turns of history.

    Delegates to paginated history with page 1 and the desired page size.
    """
    return get_history_by_turn_page(
        session_id,
        min_turn_num=1,
        turn_page_size=last_n,
        turn_page_num=1,
        only_eligible=only_eligible,
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


def get_session_ids() -> list[dict]:
    """Enumerate all distinct top-level sessions from the messages table.

    Returns one row per session, ordered by most recent activity first:

        [{
            "session_id": str,
            "last_time":   str,   # newest message timestamp (YYYYMMDDHHmmss)
            "title":       str,   # derived from the latest human message; "" when no usable text
        }, ...]

    Subagent sessions (keyed with an ``agent:<agent_id>:subagent:`` prefix
    hierarchy) are excluded in SQL, so only user-facing conversations are
    listed. The title subquery and the aggregation run as one statement.

    ``last_time`` is the newest message's ``timestamp`` text (the same
    ``YYYYMMDDHHmmss`` format used across the store).
    """
    with _db:
        rows = _db.execute("""
            SELECT
                agg.session_id,
                agg.last_time,
                (
                    SELECT m2.content
                    FROM messages m2
                    WHERE m2.session_id = agg.session_id
                      AND m2.role = 'human'
                      AND m2.origin IS NULL
                    ORDER BY m2.turn_num DESC, m2.id DESC
                    LIMIT 1
                ) AS title_content
            FROM (
                SELECT session_id, MAX(timestamp) AS last_time, MAX(ts_ms) AS last_sort
                FROM messages
                WHERE instr(session_id, ':subagent:') = 0
                GROUP BY session_id
            ) agg
            ORDER BY agg.last_sort DESC, agg.last_time DESC
        """).fetchall()

    result: list[dict] = []
    for row in rows:
        # Title comes from the latest human message of the session (the
        # user's most recent question). Background subagent-completion
        # injections (``origin = 'subagent_completion'``) are excluded, so a
        # carrier never becomes the session title; a session whose only human
        # rows are carriers gets an empty title, which the client renders as
        # an i18n placeholder (e.g. "新会话") instead of leaking the raw
        # session_id.
        result.append(
            {
                "session_id": str(row["session_id"]),
                "last_time": str(row["last_time"]),
                "title": _decode_title_content(row["title_content"]),
            }
        )
    return result
