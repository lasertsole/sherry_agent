"""Meeting persistence (spike): meetings + messages on the shared registry DB.

Option (A) from the plan §3.1: registry.db gains its own tables through the
:class:`BaseSQLiteRepository` skeleton, exactly like the todolist / taskflow /
subagent registries — one connection lifecycle, no second DB file.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any


from agent.meeting.models import MeetingMessage
from agent.tools.pub_base.sqlite_store import BaseSQLiteRepository
from config.path import resolve_registry_db_path

__all__ = ["MeetingStore", "get_meeting_store"]

_DB_PATH = resolve_registry_db_path()
_DB_DIR = _DB_PATH.parent
_BUSY_TIMEOUT_MS = 5000
_BUSY_TIMEOUT_S = _BUSY_TIMEOUT_MS / 1000.0
_INIT_WAIT_TIMEOUT_S = 10.0

# Process-level once-per-process schema-init state (the base reads these from
# this module's namespace — same contract as the todolist store).
_init_lock = asyncio.Lock()
_init_loop: asyncio.AbstractEventLoop | None = None
_initialized = False
_sync_tables_ready = False
_sync_init_lock = threading.Lock()

_TABLES_DDL = (
    """
    CREATE TABLE IF NOT EXISTS meetings (
        id TEXT PRIMARY KEY,
        session_id TEXT NOT NULL,
        topic TEXT NOT NULL,
        host_agent_id TEXT NOT NULL DEFAULT 'main',
        participants_json TEXT NOT NULL,
        status INTEGER NOT NULL DEFAULT 0,
        flow_stage TEXT NOT NULL DEFAULT 'requirement_confirmation',
        execution_plan_json TEXT,
        final_summary TEXT,
        iteration_count INTEGER NOT NULL DEFAULT 0,
        created_at INTEGER NOT NULL,
        updated_at INTEGER NOT NULL,
        ended_at INTEGER
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS meeting_messages (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        meeting_id TEXT NOT NULL,
        sequence INTEGER NOT NULL,
        speaker_kind TEXT NOT NULL,
        speaker_id TEXT NOT NULL,
        speaker_name TEXT NOT NULL,
        message_role TEXT NOT NULL DEFAULT 'normal',
        content TEXT NOT NULL,
        timestamp INTEGER NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS meeting_compaction_segments (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        meeting_id TEXT NOT NULL,
        segment_summary TEXT NOT NULL,
        start_sequence INTEGER NOT NULL,
        end_sequence INTEGER NOT NULL,
        created_at INTEGER NOT NULL
    )
    """,
)
_MESSAGES_INDEX_DDL = (
    "CREATE INDEX IF NOT EXISTS idx_meeting_messages_meeting "
    "ON meeting_messages(meeting_id, sequence)"
)
# 2026-11-24 index audit: list_meetings() does `ORDER BY created_at DESC`
# with no index at all (the table only has its TEXT PK) -> full scan + sort.
_MEETINGS_INDEX_DDL = (
    "CREATE INDEX IF NOT EXISTS idx_meetings_created_at ON meetings(created_at DESC)"
)

_STATUS = {"in_progress": 0, "waiting_user": 1, "ended": 2, "cancelled": 3}


class MeetingStore(BaseSQLiteRepository):
    """Meetings + messages CRUD over registry.db (async path only, spike)."""

    wal_label = "meeting store"

    def __init__(self) -> None:
        super().__init__(globals())

    def _table_ddls(self) -> tuple[str, ...]:
        return _TABLES_DDL

    def _index_ddls(self) -> tuple[str, ...]:
        return (_MESSAGES_INDEX_DDL, _MEETINGS_INDEX_DDL)

    @asynccontextmanager
    async def _connect(self):
        async with self.connect() as db:
            yield db

    # -- meetings ----------------------------------------------------------

    async def create_meeting(
        self,
        meeting_id: str,
        *,
        session_id: str,
        topic: str,
        participants: list[dict],
        host_agent_id: str = "main",
    ) -> dict[str, Any]:
        now = int(time.time())
        async with self._connect() as db:
            await db.execute(
                "INSERT INTO meetings(id, session_id, topic, host_agent_id, participants_json,"
                " status, flow_stage, iteration_count, created_at, updated_at)"
                " VALUES (?, ?, ?, ?, ?, 0, 'requirement_confirmation', 0, ?, ?)",
                (
                    meeting_id,
                    session_id,
                    topic,
                    host_agent_id,
                    json.dumps(participants, ensure_ascii=False),
                    now,
                    now,
                ),
            )
            await db.commit()
        return await self.get_meeting(meeting_id)

    async def get_meeting(self, meeting_id: str) -> dict[str, Any] | None:
        async with self._connect() as db:
            db.row_factory = sqlite3.Row
            cursor = await db.execute("SELECT * FROM meetings WHERE id = ?", (meeting_id,))
            row = await cursor.fetchone()
        return self._meeting_row(row) if row is not None else None

    async def list_meetings(self) -> list[dict[str, Any]]:
        async with self._connect() as db:
            db.row_factory = sqlite3.Row
            cursor = await db.execute("SELECT * FROM meetings ORDER BY created_at DESC")
            rows = await cursor.fetchall()
        return [self._meeting_row(row) for row in rows]

    async def update_meeting(self, meeting_id: str, **fields: Any) -> None:
        """Update the mutable columns (flow_stage / status / plan / summary / round)."""
        allowed = {
            "status",
            "flow_stage",
            "execution_plan_json",
            "final_summary",
            "iteration_count",
            "ended_at",
        }
        assignments = {key: value for key, value in fields.items() if key in allowed}
        if not assignments:
            return
        if "status" in assignments and isinstance(assignments["status"], str):
            assignments["status"] = _STATUS[assignments["status"]]
        if "execution_plan_json" in assignments and not isinstance(
            assignments["execution_plan_json"], str
        ):
            assignments["execution_plan_json"] = json.dumps(
                assignments["execution_plan_json"], ensure_ascii=False
            )
        assignments["updated_at"] = int(time.time())
        columns = ", ".join(f"{key} = ?" for key in assignments)
        async with self._connect() as db:
            await db.execute(
                f"UPDATE meetings SET {columns} WHERE id = ?",
                (*assignments.values(), meeting_id),
            )
            await db.commit()

    @staticmethod
    def _meeting_row(row: sqlite3.Row) -> dict[str, Any]:
        meeting = dict(row)
        meeting["participants"] = json.loads(meeting.pop("participants_json") or "[]")
        plan_raw = meeting.pop("execution_plan_json")
        meeting["execution_plan"] = json.loads(plan_raw) if plan_raw else None
        meeting["status"] = {0: "in_progress", 1: "waiting_user", 2: "ended", 3: "cancelled"}.get(
            meeting["status"], "in_progress"
        )
        return meeting

    # -- messages ----------------------------------------------------------

    async def append_message(self, message: MeetingMessage) -> None:
        async with self._connect() as db:
            await db.execute(
                "INSERT INTO meeting_messages(meeting_id, sequence, speaker_kind, speaker_id,"
                " speaker_name, message_role, content, timestamp) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    message.meeting_id,
                    message.sequence,
                    message.speaker_kind,
                    message.speaker_id,
                    message.speaker_name,
                    message.message_role,
                    message.content,
                    message.timestamp or int(time.time()),
                ),
            )
            await db.commit()

    async def list_messages(self, meeting_id: str) -> list[dict[str, Any]]:
        async with self._connect() as db:
            db.row_factory = sqlite3.Row
            cursor = await db.execute(
                "SELECT * FROM meeting_messages WHERE meeting_id = ? ORDER BY sequence ASC",
                (meeting_id,),
            )
            rows = await cursor.fetchall()
        return [dict(row) for row in rows]

    async def next_sequence(self, meeting_id: str) -> int:
        async with self._connect() as db:
            cursor = await db.execute(
                "SELECT COALESCE(MAX(sequence), 0) + 1 FROM meeting_messages WHERE meeting_id = ?",
                (meeting_id,),
            )
            row = await cursor.fetchone()
        return int(row[0]) if row else 1


_STORE: MeetingStore | None = None
_STORE_LOCK = asyncio.Lock()


async def get_meeting_store() -> MeetingStore:
    """The process-wide store (schema init races are serialized by the base)."""
    global _STORE
    if _STORE is None:
        async with _STORE_LOCK:
            if _STORE is None:
                _DB_DIR.mkdir(parents=True, exist_ok=True)
                store = MeetingStore()
                await store.ensure_db()
                _STORE = store
    return _STORE


def _reset_store_for_tests(path: Path | None = None) -> None:
    """Point the module at another DB and drop the init state (tests only)."""
    global _STORE, _DB_PATH, _DB_DIR, _initialized, _init_loop, _sync_tables_ready
    _STORE = None
    _DB_PATH = path or resolve_registry_db_path()
    _DB_DIR = _DB_PATH.parent
    _initialized = False
    _init_loop = None
    _sync_tables_ready = False
