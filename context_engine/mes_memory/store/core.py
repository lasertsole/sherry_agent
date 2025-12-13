import json
import sqlite3

from .db import get_db
from typing import Any
from datetime import datetime
from langchain_core.messages import BaseMessage

_db:sqlite3.Connection = get_db()
def add_session_if_not_exists(session_id: str) -> None:
    _db.execute("""
    INSERT OR IGNORE INTO sessions (session_id) VALUES (?)
    """, (session_id, ))

def update_session(session_id: str, params: dict[str, Any]) -> None:
    if not params:
        return

    # 黑名单：不允许更新这些字段
    block_fields = [
        "session_id",
    ]

    # 过滤出合法的字段
    valid_params = {k: v for k, v in params.items() if k not in block_fields}

    # 动态构建 SET 子句
    set_clauses = [f"{field} = ?" for field in valid_params.keys()]
    set_clause_str = ", ".join(set_clauses)

    # 构建完整的 SQL 语句
    sql = f"UPDATE sessions SET {set_clause_str} WHERE session_id = ?"

    # 参数值 + session_id
    param_values: list[Any] = list(valid_params.values()) + [session_id]

    _db.execute(sql, param_values)

async def add_messages(session_id: str, messages: list[BaseMessage]) -> None:
    if messages is None or len(messages)==0:
        return

    turn_num_row = _db.execute("""
    select MAX(turn_num) from messages where session_id = ?
    """, (session_id, )).fetchone()

    current_turn: int = turn_num_row[0] or 0
    current_turn+=1

    base_timestamp: str = datetime.now().strftime("%Y%m%d%H%M%S")

    insert_rows: list[dict] = []

    for m in messages:
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
            })
        elif m.type == "human":
            additional_kwargs:dict[str, str] = getattr(m, "additional_kwargs", {})

            # 过滤掉压缩信息的human
            if additional_kwargs.get("lc_source", None) == "summarization":
                continue

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
            })
        elif m.type == "tool":
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
            })

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
            reasoning_content
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
            :reasoning_content
        )
    """, insert_rows)

    _db.commit()

def get_messages_by_lastest_n_turns(session_id: str, last_n: int = 5)-> list[dict]:
    with _db:
        # 先获取最大的turn_num
        max_turn_num_row = _db.execute(
            "SELECT MAX(turn_num) FROM messages WHERE session_id = ?",
            (session_id,)
        ).fetchone()

        max_turn_num = max_turn_num_row[0] if max_turn_num_row and max_turn_num_row[0] is not None else 0

        # 如果最大turn_num为0，则返回空
        if max_turn_num == 0:
            return []

        # 然后查询小于阈值的记录
        threshold = max_turn_num - last_n + 1
        # 确保 threshold 至少为 1（turn_num 从 1 开始）
        if threshold < 1:
            threshold = 1

        rows = _db.execute(f"""
            SELECT * FROM messages 
            WHERE session_id = ? AND turn_num >= ?
            ORDER BY turn_num DESC, id ASC
        """, (session_id, threshold)).fetchall()
        
        if rows is None or len(rows) == 0:
            return []
        
        result:list[dict] = []
        for row in rows:
            row = dict(row)
            if isinstance(row["content"], str):
                row["content"] = json.loads(row["content"])
            if isinstance(row["tool_calls"], str):
                row["tool_calls"] = json.loads(row["tool_calls"])
            result.append(row)
        return result


def get_turns_by_turn_num_scope(session_id: str, target_turn_num: int, half_scope: int = 5) -> list[dict]:
    with _db:
        # 先获取最大的turn_num
        max_turn_row = _db.execute(
            "SELECT MAX(turn_num) FROM messages WHERE session_id = ?",
            (session_id,)
        ).fetchone()

        max_turn_num: int = max_turn_row[0] if max_turn_row and max_turn_row[0] is not None else 0
        min_turn_num: int = 1

        # 如果最大turn_num为0，则返回空
        if max_turn_num == 0:
            return []

        max_turn_num = min(max_turn_num, target_turn_num + half_scope)
        min_turn_num = max(min_turn_num, target_turn_num - half_scope)

        rows = _db.execute(f"""
            SELECT * FROM messages 
            WHERE session_id = ? AND turn_num >= ? AND turn_num <= ?
            ORDER BY turn_num DESC, id ASC
        """, (session_id, min_turn_num, max_turn_num)).fetchall()

        if rows is None or len(rows) == 0:
            return []

        result: list[dict] = []
        for row in rows:
            row = dict(row)
            if isinstance(row["content"], str):
                row["content"] = json.loads(row["content"])
            if isinstance(row["tool_calls"], str):
                row["tool_calls"] = json.loads(row["tool_calls"])
            result.append(row)

        return result