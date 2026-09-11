import re
import json
import asyncio
import sqlite3
import threading
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any
from config.features import MES_MEMORY
from loguru import logger
from pub.func import contains_cjk, count_cjk
from .store import get_db, get_messages_by_lastest_n_turns


# Lazy shared connection; created on first DB access, not at import (audit #14).
_db: sqlite3.Connection | None = None
_lock = threading.Lock()
_CONTENT_JSON_PREFIX = "\x00json:"

# FTS5 MATCH cost caps (audit #20). Measured on a 50k-row table: a 20k-term
# OR chain costs ~1s of parse/eval per query, 100k terms ~49s. SQLite offers
# no per-query limit that reaches the FTS5 parser (EXPR_DEPTH and
# LIKE_PATTERN_LENGTH leave MATCH unchanged; LENGTH is connection-global and
# would break big-content writes) — bounds are enforced on the input instead.
_MAX_QUERY_TOKENS = MES_MEMORY["max_query_tokens"]
_MAX_TOKEN_CHARS = MES_MEMORY["max_token_chars"]
_MAX_WILDCARD_TERMS = MES_MEMORY["max_wildcard_terms"]


def _shared_db() -> sqlite3.Connection:
    global _db
    if _db is None:
        _db = get_db()
    return _db


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
                            text = item.get("text", None)
                            if isinstance(text, str):
                                user_text = text
                                break
                elif isinstance(query, dict):
                    dict_text = query.get("text", None)
                    if isinstance(dict_text, str):
                        user_text = dict_text
                else:
                    user_text = query

        res_list.append(f"<turn>\nuser: {user_text}\n\nagent: {ai_text}\n</turn>")

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
    - Cap input size: token count, per-token length, wildcard terms
      (audit #20 — bounds MATCH parse/eval cost)
    """
    tokens = query.split()[:_MAX_QUERY_TOKENS]
    wildcard_seen = 0
    capped: list[str] = []
    for token in tokens:
        if "*" in token:
            wildcard_seen += 1
            if wildcard_seen > _MAX_WILDCARD_TERMS:
                token = token.replace("*", "")
        capped.append(token[:_MAX_TOKEN_CHARS])
    query = " ".join(capped)
    # Step 1: Extract balanced double-quoted phrases and protect them
    # from further processing via numbered placeholders.
    _quoted_parts: list = []

    def _preserve_quoted(m: re.Match) -> str:
        _quoted_parts.append(m.group(0))
        return f"\x00Q{len(_quoted_parts) - 1}\x00"

    sanitized = re.sub(r'"[^"]*"', _preserve_quoted, query)

    # Step 2: Strip remaining (unmatched) FTS5-special characters
    sanitized = re.sub(r"[+{}()\"^]", " ", sanitized)

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
            return json.loads(content[len(_CONTENT_JSON_PREFIX) :])
        except (json.JSONDecodeError, TypeError):
            logger.warning("Failed to decode JSON-encoded message content; returning raw string")
            return content
    return content


@dataclass(frozen=True)
class _SearchRequest:
    query: str
    session_id: str
    role_filter: list[str] | None
    limit: int
    offset: int


class _SearchStrategy(ABC):
    @abstractmethod
    def matches(self, query: str) -> bool: ...

    @abstractmethod
    def search(self, req: _SearchRequest) -> list[dict[str, Any]]: ...


class _TrigramStrategy(_SearchStrategy):
    def matches(self, query: str) -> bool:
        raw_query = query.strip('"').strip()
        if count_cjk(raw_query) < 3:
            return False
        tokens: list[str] = [
            t
            for t in raw_query.split()
            if t.upper() not in ("AND", "OR", "NOT") and contains_cjk(t)
        ]
        return not any(count_cjk(t) < 3 for t in tokens)

    def search(self, req: _SearchRequest) -> list[dict[str, Any]]:
        # Trigram FTS5 path — quote each non-operator token to handle
        # FTS5 special chars (%, *, etc.) while preserving boolean
        # operators (AND, OR, NOT) for multi-term queries.
        raw_query = req.query.strip('"').strip()
        tokens: list[str] = raw_query.split()
        parts: list[str] = []
        for tok in tokens:
            if tok.upper() in ("AND", "OR", "NOT"):
                parts.append(tok)
            else:
                parts.append('"' + tok.replace('"', '""') + '"')
        trigram_query = " ".join(parts)
        tri_where = ["m.session_id = ?", "messages_fts_trigram MATCH ?"]
        tri_params: list = [req.session_id, trigram_query]
        if req.role_filter:
            tri_where.append(f"m.role IN ({','.join('?' for _ in req.role_filter)})")
            tri_params.extend(req.role_filter)
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
            WHERE {" AND ".join(tri_where)}
            ORDER BY rank
            LIMIT ? OFFSET ?
        """
        tri_params.extend([req.limit, req.offset])
        with _lock:
            try:
                tri_cursor = _shared_db().execute(tri_sql, tri_params)
            except sqlite3.OperationalError:
                matches = []
            else:
                matches = [dict(row) for row in tri_cursor.fetchall()]
        return matches


class _LikeStrategy(_SearchStrategy):
    def matches(self, query: str) -> bool:
        if not contains_cjk(query):
            return False
        return not _TrigramStrategy().matches(query)

    def search(self, req: _SearchRequest) -> list[dict[str, Any]]:
        # Short / mixed CJK query: trigram cannot match tokens with
        # <3 CJK chars. Fall back to LIKE substring search.
        # For multi-token OR queries (e.g. "广西 OR 桂林 OR 漓江"),
        # build one LIKE condition per non-operator token so each term
        # is matched independently (#20494).
        raw_query = req.query.strip('"').strip()
        non_op_tokens = [t for t in raw_query.split() if t.upper() not in ("AND", "OR", "NOT")] or [
            raw_query
        ]
        token_clauses = []
        # NOTE: SQL placeholder order: instr(?) is FIRST (in SELECT),
        # then m.session_id = ? (in WHERE), then LIKE values, then
        # role_filter, then LIMIT/OFFSET.  Parameter list must match.
        like_params: list = [non_op_tokens[0]]  # for instr(?) — goes first
        like_params.append(req.session_id)  # for m.session_id = ?
        for tok in non_op_tokens:
            esc = tok.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            token_clauses.append(
                "(m.content LIKE ? ESCAPE '\\' OR m.tool_name LIKE ? ESCAPE '\\' OR m.tool_calls LIKE ? ESCAPE '\\')"
            )
            like_params += [f"%{esc}%", f"%{esc}%", f"%{esc}%"]
        like_where = ["m.session_id = ?", f"({' OR '.join(token_clauses)})"]
        if req.role_filter:
            like_where.append(f"m.role IN ({','.join('?' for _ in req.role_filter)})")
            like_params.extend(req.role_filter)
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
            WHERE {" AND ".join(like_where)}
            ORDER BY m.timestamp DESC
            LIMIT ? OFFSET ?
        """
        like_params.extend([req.limit, req.offset])
        with _lock:
            like_cursor = _shared_db().execute(like_sql, like_params)
            return [dict(row) for row in like_cursor.fetchall()]


class _FtsStrategy(_SearchStrategy):
    def matches(self, query: str) -> bool:
        # CJK queries bypass the unicode61 FTS5 table.  The default tokenizer
        # splits CJK characters into individual tokens, so "大别山项目" becomes
        # "大 AND 别 AND 山 AND 项 AND 目" — producing false positives and
        # missing exact phrase matches.
        return not contains_cjk(query)

    def search(self, req: _SearchRequest) -> list[dict[str, Any]]:
        with _lock:
            try:
                where_clauses = ["m.session_id = ?", "messages_fts MATCH ?"]
                params: list = [req.session_id, req.query]

                if req.role_filter:
                    role_placeholders = ",".join("?" for _ in req.role_filter)
                    where_clauses.append(f"m.role IN ({role_placeholders})")
                    params.extend(req.role_filter)

                params.extend([req.limit, req.offset])

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
                cursor = _shared_db().execute(sql, params)
            except sqlite3.OperationalError:
                return []
            else:
                return [dict(row) for row in cursor.fetchall()]


class _SearchStrategyFactory:
    def __init__(self, strategies: list[_SearchStrategy]) -> None:
        self._strategies = strategies

    def select(self, query: str) -> _SearchStrategy:
        for strategy in self._strategies:
            if strategy.matches(query):
                return strategy
        return self._strategies[-1]


_SEARCH_STRATEGY_FACTORY = _SearchStrategyFactory(
    [_TrigramStrategy(), _LikeStrategy(), _FtsStrategy()]
)


def _attach_search_context(matches: list[dict[str, Any]], session_id: str) -> None:
    """Add surrounding context (1 message before + after each match)."""
    if not matches:
        return
    values_rows: list[str] = []
    ctx_params: list = []
    for ord_idx, match in enumerate(matches):
        values_rows.append("(?, ?)")
        ctx_params.extend([ord_idx, match["id"]])
    ctx_params.append(session_id)
    ctx_sql = f"""
        WITH t(ord, mid) AS (VALUES {", ".join(values_rows)}),
        sess AS (
            SELECT m.id, m.role, m.content,
                   ROW_NUMBER() OVER (ORDER BY m.timestamp, m.id) AS g
            FROM messages m
            WHERE m.session_id = ?
        ),
        tg AS (
            SELECT t.ord, s.g
            FROM t
            JOIN sess s ON s.id = t.mid
        ),
        want AS (
            SELECT ord, 0 AS pos, g - 1 AS gr FROM tg
            UNION ALL
            SELECT ord, 1 AS pos, g AS gr FROM tg
            UNION ALL
            SELECT ord, 2 AS pos, g + 1 AS gr FROM tg
        )
        SELECT w.ord, w.pos, s.role, s.content
        FROM want w
        JOIN sess s ON s.g = w.gr
        ORDER BY w.ord, w.pos
    """
    try:
        with _lock:
            ctx_rows = _shared_db().execute(ctx_sql, ctx_params).fetchall()
        for match in matches:
            match["context"] = []
        for r in ctx_rows:
            raw = r["content"]
            decoded = _decode_content(raw)
            # Multimodal context: render a compact text-only
            # summary for search previews.
            if isinstance(decoded, list):
                text_parts = [
                    p.get("text", "")
                    for p in decoded
                    if isinstance(p, dict) and p.get("type") == "text"
                ]
                text = " ".join(t for t in text_parts if t).strip()
                preview = text or "[multimodal content]"
            elif isinstance(decoded, str):
                preview = decoded
            else:
                preview = ""
            matches[r["ord"]]["context"].append({"role": r["role"], "content": preview[:200]})
    except Exception:
        for match in matches:
            match["context"] = []


def search_messages(
    query: str,
    session_id: str,
    role_filter: list[str] | None = None,
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

    req = _SearchRequest(
        query=query,
        session_id=session_id,
        role_filter=role_filter,
        limit=limit,
        offset=offset,
    )
    matches = _SEARCH_STRATEGY_FACTORY.select(query).search(req)

    elapsed = time.time() - start_time
    logger.debug(
        f"Message search completed: session_id={session_id}, "
        f"match_count={len(matches)}, duration={elapsed:.3f}s"
    )

    _attach_search_context(matches, session_id)

    # Remove full content from result (snippet is enough, saves tokens)
    for match in matches:
        match.pop("content", None)

    return matches


async def search_messages_async(
    query: str,
    session_id: str,
    role_filter: list[str] | None = None,
    limit: int = 20,
    offset: int = 0,
) -> list[dict[str, Any]]:
    """Async entrypoint: runs the blocking FTS5/LIKE search in an executor thread.

    The threading.Lock and sqlite3 I/O then live on a worker thread, so the
    event loop stays responsive (audit #14).
    """
    return await asyncio.to_thread(
        search_messages,
        query,
        session_id,
        role_filter=role_filter,
        limit=limit,
        offset=offset,
    )
