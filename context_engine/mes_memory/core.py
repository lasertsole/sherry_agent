import re
import json
import sqlite3
import textwrap
import threading
from typing import Any
from loguru import logger
from itertools import groupby
from models import simple_chat_model
from langchain.agents import create_agent
from config import ASSISTANT_NAME, USER_NAME
from pub_func import contains_cjk, count_cjk
from langgraph.graph.state import CompiledStateGraph
from langchain_core.messages import AIMessage, HumanMessage, BaseMessage
from .store import get_db, get_messages_by_lastest_n_turns, update_session, add_messages


_db: sqlite3.Connection = get_db()
_lock = threading.Lock()
_CONTENT_JSON_PREFIX = "\x00json:"

def retrieve_history_by_last_n_prompt(session_id: str, n: int = 5) -> str:
    result: list[dict] = get_messages_by_lastest_n_turns(session_id, n)

    order_dict = {}
    for i in result:
        if i.get("turn_num") is None:
            continue
        if i["turn_num"] in order_dict:
            order_dict[i["turn_num"]].append(i)
        else:
            order_dict[i["turn_num"]] = [i]

    res_list: list[str] = []
    for key, mes_list in order_dict.items():
        mes_list.sort(key=lambda x: x.get("id"))

        ai_text: str = ""
        user_text: str = ""

        for mes in mes_list:
            if mes.get("role") == "ai":
                ai_text += mes.get("content")
            elif mes.get("role") == "human":
                query = mes.get("content")

                # Get user input
                if query is None:
                    user_text = ""
                elif isinstance(query, list):
                    user_text = ""
                    for item in query:
                        if item.get("type", None) == "text":
                            user_text = item.get("text", None)
                            break
                elif isinstance(query, dict):
                    user_text = query.get("text", None)
                else:
                    user_text = query

        res_list.append(f"<turn>\n{USER_NAME}: {user_text}\n\n{ASSISTANT_NAME}: {ai_text}\n</turn>")

    return (
        f"===== The following is the content of the last {n} turns (from oldest to newest, timestamp format: YYYYMMDDHHmmss) =====\n\n"
        f"{'\n\n'.join([item for item in res_list])}"
        f"\n\n===== The above is the content of the last {n} turns =====\n\n"
    )

def _sanitize_fts5_query(query: str) -> str:
    """Sanitize user input for safe use in FTS5 MATCH queries.

    FTS5 has its own query syntax where characters like ``"``, ``(``, ``)``,
    ``+``, ``*``, ``{``, ``}`` and bare boolean operators (``AND``, ``OR``,
    ``NOT``) have special meaning.  Passing raw user input directly to
    MATCH can cause ``sqlite3.OperationalError``.

    Strategy:
    - Preserve properly paired quoted phrases (``"exact phrase"``)
    - Strip unmatched FTS5-special characters that would cause errors
    - Wrap unquoted hyphenated and dotted terms in quotes so FTS5
      matches them as exact phrases instead of splitting on the
      hyphen/dot (e.g. ``chat-send``, ``P2.2``, ``my-app.config.ts``)
    """
    # Step 1: Extract balanced double-quoted phrases and protect them
    # from further processing via numbered placeholders.
    _quoted_parts: list = []

    def _preserve_quoted(m: re.Match) -> str:
        _quoted_parts.append(m.group(0))
        return f"\x00Q{len(_quoted_parts) - 1}\x00"

    sanitized = re.sub(r'"[^"]*"', _preserve_quoted, query)

    # Step 2: Strip remaining (unmatched) FTS5-special characters
    sanitized = re.sub(r'[+{}()\"^]', " ", sanitized)

    # Step 3: Collapse repeated * (e.g. "***") into a single one,
    # and remove leading * (prefix-only needs at least one char before *)
    sanitized = re.sub(r"\*+", "*", sanitized)
    sanitized = re.sub(r"(^|\s)\*", r"\1", sanitized)

    # Step 4: Remove dangling boolean operators at start/end that would
    # cause syntax errors (e.g. "hello AND" or "OR world")
    sanitized = re.sub(r"(?i)^(AND|OR|NOT)\b\s*", "", sanitized.strip())
    sanitized = re.sub(r"(?i)\s+(AND|OR|NOT)\s*$", "", sanitized.strip())

    # Step 5: Wrap unquoted dotted and/or hyphenated terms in double
    # quotes.  FTS5's tokenizer splits on dots and hyphens, turning
    # ``chat-send`` into ``chat AND send`` and ``P2.2`` into ``p2 AND 2``.
    # Quoting preserves phrase semantics.  A single pass avoids the
    # double-quoting bug that would occur if dotted, hyphenated and underscored
    # patterns were applied sequentially (e.g. ``my-app.config``).
    sanitized = re.sub(r"\b(\w+(?:[._-]\w+)+)\b", r'"\1"', sanitized)

    # Step 6: Restore preserved quoted phrases
    for i, quoted in enumerate(_quoted_parts):
        sanitized = sanitized.replace(f"\x00Q{i}\x00", quoted)

    return sanitized.strip()

def _decode_content(content: Any) -> Any:
    """Reverse :meth:`_encode_content`; returns scalars unchanged."""
    if isinstance(content, str) and content.startswith(_CONTENT_JSON_PREFIX):
        try:
            return json.loads(content[len(_CONTENT_JSON_PREFIX):])
        except (json.JSONDecodeError, TypeError):
            logger.warning(
                "Failed to decode JSON-encoded message content; "
                "returning raw string"
            )
            return content
    return content

def search_messages(
    query: str,
    session_id: str,
    role_filter: list[str] = None,
    limit: int = 20,
    offset: int = 0,
) -> list[dict[str, Any]]:
    import time
    start_time = time.time()
    
    logger.debug(
        f"Searching messages: session_id={session_id}, query='{query[:50]}', "
        f"limit={limit}, offset={offset}"
    )
    
    if not query or not query.strip():
        logger.debug("Search query is empty")
        return []

    query = _sanitize_fts5_query(query)
    if not query:
        logger.debug("Search query sanitized to empty")
        return []

    # CJK queries bypass the unicode61 FTS5 table.  The default tokenizer
    # splits CJK characters into individual tokens, so "大别山项目" becomes
    # "大 AND 别 AND 山 AND 项 AND 目" — producing false positives and
    # missing exact phrase matches.
    #
    # For queries with 3+ CJK characters, we use the trigram FTS5 table
    # (indexed substring matching with ranking and snippets).  For shorter
    # CJK queries (1-2 chars), trigram can't match (it needs ≥9 UTF-8
    # bytes = 3 CJK chars), so we fall back to LIKE.
    is_cjk = contains_cjk(query)

    if is_cjk:
        raw_query:str = query.strip('"').strip()
        cjk_count:int = count_cjk(raw_query)

        # Per-token CJK length check (#20494): trigram needs >=3 CJK chars
        # per token. A query like "广西 OR 桂林 OR 漓江" has cjk_count=6
        # (>=3) but each individual token is only 2 chars — trigram returns 0.
        # Route to LIKE when any non-operator CJK token is <3 CJK chars.
        _tokens_for_check: list[str] = [
            t for t in raw_query.split()
            if t.upper() not in ("AND", "OR", "NOT") and contains_cjk(t)
        ]
        _any_short_cjk:bool = any(
            count_cjk(t) < 3 for t in _tokens_for_check
        )

        if cjk_count >= 3 and not _any_short_cjk:
            # Trigram FTS5 path — quote each non-operator token to handle
            # FTS5 special chars (%, *, etc.) while preserving boolean
            # operators (AND, OR, NOT) for multi-term queries.
            tokens:list[str] = raw_query.split()
            parts: list[str] = []
            for tok in tokens:
                if tok.upper() in ("AND", "OR", "NOT"):
                    parts.append(tok)
                else:
                    parts.append('"' + tok.replace('"', '""') + '"')
            trigram_query = " ".join(parts)
            tri_where = ["m.session_id = ?", "messages_fts_trigram MATCH ?"]
            tri_params: list = [session_id, trigram_query]
            if role_filter:
                tri_where.append(f"m.role IN ({','.join('?' for _ in role_filter)})")
                tri_params.extend(role_filter)
            tri_sql = f"""
                SELECT
                    m.id,
                    m.session_id,
                    m.turn_num,
                    m.role,
                    snippet(messages_fts_trigram, 0, '>>>', '<<<', '...', 40) AS snippet,
                    m.content,
                    m.timestamp,
                    m.tool_name
                FROM messages_fts_trigram
                JOIN messages m ON m.id = messages_fts_trigram.rowid
                WHERE {' AND '.join(tri_where)}
                ORDER BY rank
                LIMIT ? OFFSET ?
            """
            tri_params.extend([limit, offset])
            with _lock:
                try:
                    tri_cursor = _db.execute(tri_sql, tri_params)
                except sqlite3.OperationalError:
                    matches = []
                else:
                    matches = [dict(row) for row in tri_cursor.fetchall()]
        else:
            # Short / mixed CJK query: trigram cannot match tokens with
            # <3 CJK chars. Fall back to LIKE substring search.
            # For multi-token OR queries (e.g. "广西 OR 桂林 OR 漓江"),
            # build one LIKE condition per non-operator token so each term
            # is matched independently (#20494).
            non_op_tokens = [
                t for t in raw_query.split()
                if t.upper() not in ("AND", "OR", "NOT")
            ] or [raw_query]
            token_clauses = []
            like_params: list = [session_id]  # for m.session_id = ?
            for tok in non_op_tokens:
                esc = tok.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
                token_clauses.append(
                    "(m.content LIKE ? ESCAPE '\\' OR m.tool_name LIKE ? ESCAPE '\\' OR m.tool_calls LIKE ? ESCAPE '\\')"
                )
                like_params += [f"%{esc}%", f"%{esc}%", f"%{esc}%"]
            like_where = ["m.session_id = ?", f"({' OR '.join(token_clauses)})"]
            if role_filter:
                like_where.append(f"m.role IN ({','.join('?' for _ in role_filter)})")
                like_params.extend(role_filter)
            # instr() for snippet uses first search token
            like_params.append(non_op_tokens[0])
            like_sql = f"""
                SELECT 
                m.id,
                m.session_id,
                m.turn_num,
                m.role,
                substr(m.content,
                    max(1, instr(m.content, ?) - 40),
                    120
                ) AS snippet,
                m.content, m.timestamp, m.tool_name
                FROM messages m
                WHERE {' AND '.join(like_where)}
                ORDER BY m.timestamp DESC
                LIMIT ? OFFSET ?
            """
            like_params.extend([limit, offset])
            with _lock:
                like_cursor = _db.execute(like_sql, like_params)
                matches = [dict(row) for row in like_cursor.fetchall()]
    else:
        with _lock:
            try:
                # Build WHERE clauses dynamically
                where_clauses = ["m.session_id = ?", "messages_fts MATCH ?"]
                params: list = [session_id, query]

                if role_filter:
                    role_placeholders = ",".join("?" for _ in role_filter)
                    where_clauses.append(f"m.role IN ({role_placeholders})")
                    params.extend(role_filter)

                params.extend([limit, offset])

                where_sql = " AND ".join(where_clauses)
                sql = f"""
                    SELECT
                        m.id,
                        m.session_id,
                        m.turn_num,
                        m.role,
                        snippet(messages_fts, 0, '>>>', '<<<', '...', 40) AS snippet,
                        m.content,
                        m.timestamp,
                        m.tool_name
                    FROM messages_fts
                    JOIN messages m ON m.id = messages_fts.rowid
                    WHERE {where_sql}
                    ORDER BY rank
                    LIMIT ? OFFSET ?
                """
                cursor = _db.execute(sql, params)
            except sqlite3.OperationalError:
                # FTS5 query syntax error despite sanitization — return empty
                return []
            else:
                matches = [dict(row) for row in cursor.fetchall()]

    elapsed = time.time() - start_time
    logger.debug(
        f"Message search completed: session_id={session_id}, "
        f"match_count={len(matches)}, duration={elapsed:.3f}s"
    )

    # Add surrounding context (1 message before + after each match).
    # Done outside the lock so we don't hold it across N sequential queries.
    for match in matches:
        try:
            with _lock:
                ctx_cursor = _db.execute(
                    """WITH target AS (
                           SELECT session_id, timestamp, id
                           FROM messages
                           WHERE id = ?
                       )
                       SELECT role, content
                       FROM (
                           SELECT m.id, m.timestamp, m.role, m.content
                           FROM messages m
                           JOIN target t ON t.session_id = m.session_id
                           WHERE (m.timestamp < t.timestamp)
                              OR (m.timestamp = t.timestamp AND m.id < t.id)
                           ORDER BY m.timestamp DESC, m.id DESC
                           LIMIT 1
                       )
                       UNION ALL
                       SELECT role, content
                       FROM messages
                       WHERE id = ?
                       UNION ALL
                       SELECT role, content
                       FROM (
                           SELECT m.id, m.timestamp, m.role, m.content
                           FROM messages m
                           JOIN target t ON t.session_id = m.session_id
                           WHERE (m.timestamp > t.timestamp)
                              OR (m.timestamp = t.timestamp AND m.id > t.id)
                           ORDER BY m.timestamp ASC, m.id ASC
                           LIMIT 1
                       )""",
                    (match["id"], match["id"]),
                )
                context_msgs = []
                for r in ctx_cursor.fetchall():
                    raw = r["content"]
                    decoded = _decode_content(raw)
                    # Multimodal context: render a compact text-only
                    # summary for search previews.
                    if isinstance(decoded, list):
                        text_parts = [
                            p.get("text", "") for p in decoded
                            if isinstance(p, dict) and p.get("type") == "text"
                        ]
                        text = " ".join(t for t in text_parts if t).strip()
                        preview = text or "[multimodal content]"
                    elif isinstance(decoded, str):
                        preview = decoded
                    else:
                        preview = ""
                    context_msgs.append(
                        {"role": r["role"], "content": preview[:200]}
                    )
            match["context"] = context_msgs
        except Exception:
            match["context"] = []

    # Remove full content from result (snippet is enough, saves tokens)
    for match in matches:
        match.pop("content", None)

    return matches

async def append_messages(session_id: str, messages: list[BaseMessage], nudge_turn:int = 10) -> None:
    await add_messages(session_id, messages)

    await nudge_messages(session_id = session_id, nudge_turn = nudge_turn)

async def nudge_messages(session_id: str, skip_last_turn: bool = False, nudge_turn:int = 10) -> None:
    session_row = _db.execute("""
    select nudge_turn_num from sessions where session_id = ?
    """, (session_id,)).fetchone()

    session_dict: dict[str, int] = dict(session_row)

    nudge_turn_num: int = session_dict["nudge_turn_num"] or 0

    turn_num_row = _db.execute("""
    select MAX(turn_num) from messages where session_id = ?
    """, (session_id, )).fetchone()
    turn_num: int = turn_num_row[0] or 1

    if turn_num - nudge_turn_num < nudge_turn:
        return

    gap: int = turn_num - nudge_turn_num

    if gap > 0:
        messages: list[dict] = get_messages_by_lastest_n_turns(session_id, gap)

        filter_messages: list[dict] = []
        for m in messages:
            if skip_last_turn and m.get("turn_num") == turn_num:
                continue

            role: str = m.get("role")
            if role != "ai" and role != "human":
                continue
            filter_messages.append(m)

        filter_messages.sort(key=lambda x: x.get("turn_num", 0))

        final_messages: list[BaseMessage] = []

        for t_num, turn_group in groupby(filter_messages, key=lambda x: x.get("turn_num", 0)):
            for role, role_group in groupby(list(turn_group), key=lambda x: x.get("role")):
                combined_content = "".join([m.get("content", "") for m in role_group])

                if role == "ai":
                    final_messages.append(AIMessage(content=combined_content))
                elif role == "human":
                    final_messages.append(HumanMessage(content=combined_content))

        from tools import build_memory_tool, memory_store

        system_prompt: str = "\n\n".join([
            """
                Below are the existing user preferences (Earlier entries are at the top). 
            """,
            memory_store.format_for_system_prompt("memory"),
            memory_store.format_for_system_prompt("user")
        ])
        extract_memory_agent: CompiledStateGraph = create_agent(
            model = simple_chat_model,
            system_prompt = system_prompt,
            tools = [build_memory_tool(session_id)],
        )

        # invoke
        final_messages.append(HumanMessage(content=textwrap.dedent("""\
            Review the historical chat records and call the 'memory' tool to record the user's preferences one by one.
            Please add, update, or replace them based on the situation. Do not add duplicate preferences that already exist.
            If the user preferences are full, merge them as appropriate. In the worst case, remove earlier preferences to make room for new ones. (Earlier entries are at the top)
            Note: 1.If you notice that the user has already updated certain preferences via the 'memory' tool in the chat history, avoid duplicating those updates.
                  2.Avoid storing irrelevant data (e.g., tool execution logs or results) in the memory entries.
        """)))

        await extract_memory_agent.ainvoke(input={"messages": final_messages})

        # update nudge turn num
        update_session(session_id, {"nudge_turn_num": turn_num})