"""SQLite persistence layer: serializes SubagentRunRecord instances as JSON into aiosqlite.

Database path: subagent/data/subagent_registry.db
Table schema: subagent_runs(run_id TEXT PK, data TEXT) where data is model_dump_json()
"""

import json
from pathlib import Path
from loguru import logger
from typing import Any
import aiosqlite
from ..types.registry import (
    SubagentRunRecord,
    ExecutionState,
    CompletionState,
    CompletionDeliveryState,
    RunOutcome,
    ExecutionStatus,
    DeliveryStatus,
    RunOutcomeStatus,
)
from ..types.spawn import SpawnMode, ContextMode
from ..types.capability import SubagentSessionRole, ControlScope

_DB_DIR = Path(__file__).resolve().parent.parent / "data"
_DB_PATH = _DB_DIR / "subagent_registry.db"

_CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS subagent_runs (
    run_id TEXT PRIMARY KEY,
    data TEXT NOT NULL
);
"""

_CREATE_SETTLE_WAKE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS settle_wake_state (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    data TEXT NOT NULL
);
"""


def _serialize_run(run: SubagentRunRecord) -> str:
    """Serialize a run record to a JSON string."""
    return run.model_dump_json()


def _deserialize_run(data: str) -> SubagentRunRecord:
    """Deserialize a JSON string back into a SubagentRunRecord."""
    return SubagentRunRecord.model_validate_json(data)


async def ensure_db() -> None:
    """Ensure the database directory and required tables exist."""
    _DB_DIR.mkdir(parents=True, exist_ok=True)
    async with aiosqlite.connect(_DB_PATH) as db:
        await db.execute(_CREATE_TABLE_SQL)
        await db.commit()


async def save_runs_to_sqlite(runs: dict[str, SubagentRunRecord]) -> None:
    """Full-replace write of all run records (DELETE then INSERT)."""
    await ensure_db()
    async with aiosqlite.connect(_DB_PATH) as db:
        await db.execute("DELETE FROM subagent_runs")
        for run_id, run in runs.items():
            await db.execute(
                "INSERT INTO subagent_runs (run_id, data) VALUES (?, ?)",
                (run_id, _serialize_run(run)),
            )
        await db.commit()


async def load_runs_from_sqlite() -> dict[str, SubagentRunRecord]:
    """Load all run records from SQLite; records that fail deserialization are skipped."""
    await ensure_db()
    runs: dict[str, SubagentRunRecord] = {}
    try:
        async with aiosqlite.connect(_DB_PATH) as db:
            async with db.execute("SELECT run_id, data FROM subagent_runs") as cursor:
                async for row in cursor:
                    run_id, data = row
                    try:
                        runs[run_id] = _deserialize_run(data)
                    except Exception as e:
                        logger.warning("Failed to deserialize run {}: {}", run_id, e)
    except Exception as e:
        logger.warning("Failed to load from SQLite: {}", e)
    return runs


async def upsert_run_to_sqlite(run: SubagentRunRecord) -> None:
    """Upsert a single run record for incremental persistence."""
    await ensure_db()
    async with aiosqlite.connect(_DB_PATH) as db:
        await db.execute(
            "INSERT OR REPLACE INTO subagent_runs (run_id, data) VALUES (?, ?)",
            (run.run_id, _serialize_run(run)),
        )
        await db.commit()


async def delete_run_from_sqlite(run_id: str) -> None:
    """Delete a single run record from SQLite by run_id."""
    await ensure_db()
    async with aiosqlite.connect(_DB_PATH) as db:
        await db.execute("DELETE FROM subagent_runs WHERE run_id = ?", (run_id,))
        await db.commit()


def save_settle_wake_state(state: dict) -> None:
    """Synchronously save settle-wake state to SQLite."""
    import sqlite3
    _DB_DIR.mkdir(parents=True, exist_ok=True)
    try:
        conn = sqlite3.connect(str(_DB_PATH))
        conn.execute(_CREATE_SETTLE_WAKE_TABLE_SQL)
        conn.execute(
            "INSERT OR REPLACE INTO settle_wake_state (id, data) VALUES (1, ?)",
            (json.dumps(state),),
        )
        conn.commit()
        conn.close()
    except Exception as e:
        logger.debug("Failed to save settle-wake state: {}", e)


def load_settle_wake_state() -> dict | None:
    """Synchronously load settle-wake state from SQLite."""
    import sqlite3
    try:
        _DB_DIR.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(_DB_PATH))
        conn.execute(_CREATE_SETTLE_WAKE_TABLE_SQL)
        row = conn.execute("SELECT data FROM settle_wake_state WHERE id = 1").fetchone()
        conn.close()
        if row:
            return json.loads(row[0])
    except Exception as e:
        logger.debug("Failed to load settle-wake state: {}", e)
    return None
