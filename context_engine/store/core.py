import json
import sqlite3
from .db import get_db
from typing import Annotated
from datetime import datetime
from pydantic import Field, validate_call
from langchain_core.messages import BaseMessage


_db:sqlite3.Connection = get_db()

def get_max_turn_num(session_id: str) -> int:
    """Get max turn_num in session"""
    max_turn_num_row = _db.execute(
        "SELECT MAX(turn_num) FROM messages WHERE session_id = ?",
        (session_id,)
    ).fetchone()
    return max_turn_num_row[0] if max_turn_num_row and max_turn_num_row[0] is not None else 0

async def add_messages(session_id: str, messages: list[BaseMessage]) -> None:
    if messages is None or len(messages)==0:
        return

    current_turn: int = get_max_turn_num(session_id)
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

def get_turns_by_turn_num_scope(session_id: str, target_turn_num: int, half_scope: int = 5) -> list[dict]:
    with _db:
        max_turn_num: int = get_max_turn_num(session_id)
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

@validate_call
def get_history_by_page(
    session_id: str,
    min_turn_num: Annotated[int, Field(ge=1)] = 1,
    turn_page_size: Annotated[int, Field(ge=1)] = 10,
    turn_page_num: Annotated[int, Field(ge=1)] = 1,
) -> list[dict]:
    with _db:
        max_turn_num: int = get_max_turn_num(session_id)

        if max_turn_num == 0:
            return []

        target_end_turn_num: int = max_turn_num - (turn_page_num - 1) * turn_page_size

        target_start_turn_num: int = target_end_turn_num - turn_page_size + 1

        if target_start_turn_num < min_turn_num:
            target_start_turn_num = min_turn_num

        rows = _db.execute("""
            select * from messages
            where session_id = ? and turn_num >= ? and turn_num <= ?
            ORDER BY turn_num DESC, id ASC
        """, (session_id, target_start_turn_num, target_end_turn_num)).fetchall()

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


def get_messages_by_lastest_n_turns(session_id: str, last_n: int = 5) -> list[dict]:
    return get_history_by_page(session_id, min_turn_num=1, turn_page_size=last_n, turn_page_num=1)