"""One-time boot migration for the shared registry SQLite file.

Historically the subagent registry, TaskFlow, todolist and evidence ledger each
owned a database in their own directory. They are now tables of one
``SRC_DIR/data/registry.db``. This module copies every table from the legacy
files into the shared file (column-intersection, so an older schema missing a
new column still migrates), and renames each legacy file to ``*.bak`` only
after its copy committed.

Idempotent: a crash after commit but before the rename re-runs the copy, whose
``INSERT OR IGNORE`` skips rows that already landed. The caller (server boot)
treats any failure as non-fatal and leaves the legacy files untouched.
"""

from __future__ import annotations

import sqlite3
import time
from collections.abc import Iterable
from pathlib import Path

from loguru import logger

from config.path import ROOT_DIR, SRC_DIR, resolve_registry_db_path

__all__ = ["migrate_legacy_registry_dbs"]


def _legacy_defaults() -> tuple[Path, ...]:
    root = Path(ROOT_DIR)
    return (
        root / "agent" / "tools" / "subagent" / "data" / "subagent_registry.db",
        root / "agent" / "tools" / "taskflow" / "data" / "taskflow_registry.db",
        root / "agent" / "tools" / "todolist" / "data" / "todos.db",
        Path(SRC_DIR) / "data" / "evidence-ledger.db",
    )


def _schema_ddls() -> tuple[str, ...]:
    """The CURRENT schema of every table that shares the registry file."""
    from agent.tools.subagent.registry import pending_injections as pending
    from agent.tools.subagent.registry import store_sqlite as subagent
    from agent.tools.taskflow.registry import store_sqlite as taskflow
    from agent.tools.todolist.registry import store_sqlite as todolist
    from server.queue import user_input_queue as queue

    sub_repo = subagent._repository
    flow_repo = taskflow._repository
    todo_repo = todolist._repository
    return (
        *sub_repo._sync_table_ddls(),
        *sub_repo._index_ddls(),
        *flow_repo._sync_table_ddls(),
        *flow_repo._index_ddls(),
        *todo_repo._sync_table_ddls(),
        *todo_repo._index_ddls(),
        pending._CREATE_TABLE_SQL,
        pending._CREATE_STATUS_INDEX_SQL,
        queue._CREATE_TABLE_SQL,
        *queue._CREATE_INDEX_SQLS,
    )


def _evidence_schema() -> str:
    from agent.tools.pub_base.evidence_ledger import _SCHEMA

    return _SCHEMA


def _table_names(conn: sqlite3.Connection, schema: str) -> set[str]:
    rows = conn.execute(
        f"SELECT name FROM {schema}.sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
    ).fetchall()
    return {str(row[0]) for row in rows}


def _column_info(conn: sqlite3.Connection, schema: str, table: str) -> list[tuple[str, str]]:
    """``(name, normalized declared type)`` for one table."""
    return [
        (str(row[1]), str(row[2] or "").strip().upper())
        for row in conn.execute(f'PRAGMA "{schema}".table_info("{table}")')
    ]


def _backup_path(path: Path) -> Path:
    candidate = path.with_name(f"{path.name}.bak")
    if not candidate.exists():
        return candidate
    return path.with_name(f"{path.name}.{int(time.time())}.bak")


def _copy_legacy(conn: sqlite3.Connection, legacy: Path, tables: set[str]) -> dict[str, int]:
    """Copy the tables this file actually has into ``main``; counts per table."""
    copied: dict[str, int] = {}
    conn.execute("ATTACH DATABASE ? AS legacy", (str(legacy),))
    try:
        old_tables = _table_names(conn, "legacy")
        for table in sorted(old_tables & tables):
            old_info = _column_info(conn, "legacy", table)
            new_info = _column_info(conn, "main", table)
            old_cols = [name for name, _type in old_info]
            new_cols = [name for name, _type in new_info]
            cols = [name for name in new_cols if name in old_cols]
            if not cols:
                raise ValueError(
                    f"legacy table {table!r} shares no columns with the current schema"
                )
            old_types = dict(old_info)
            new_types = dict(new_info)
            for name in cols:
                if old_types[name] != new_types[name]:
                    raise ValueError(
                        f"legacy table {table!r} column {name!r} has type "
                        f"{old_types[name]!r}, current schema has {new_types[name]!r}"
                    )
            column_sql = ", ".join(f'"{name}"' for name in cols)
            before = conn.total_changes
            conn.execute("BEGIN IMMEDIATE")
            try:
                conn.execute(
                    f'INSERT OR IGNORE INTO main."{table}" ({column_sql}) '
                    f'SELECT {column_sql} FROM legacy."{table}"'
                )
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise
            copied[table] = conn.total_changes - before
    finally:
        conn.execute("DETACH DATABASE legacy")
    return copied


def migrate_legacy_registry_dbs(
    *,
    registry_path: Path | None = None,
    legacy_paths: Iterable[Path] | None = None,
) -> dict[str, dict[str, int]]:
    """Copy every legacy DB into the shared registry file; return per-file counts.

    A legacy file is renamed to ``*.bak`` ONLY after its tables committed. A
    failure on one file is logged and leaves that file in place; the remaining
    files still migrate. The shared file itself is never deleted or truncated.
    """
    registry = Path(registry_path) if registry_path is not None else resolve_registry_db_path()
    registry.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(registry), timeout=5.0, isolation_level=None)
    report: dict[str, dict[str, int]] = {}
    try:
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute("PRAGMA journal_mode=WAL")
        for ddl in _schema_ddls():
            conn.execute(ddl)
        conn.executescript(_evidence_schema())
        tables = _table_names(conn, "main")

        for legacy in (Path(p) for p in (legacy_paths or _legacy_defaults())):
            if not legacy.is_file() or legacy.resolve() == registry.resolve():
                continue
            try:
                copied = _copy_legacy(conn, legacy, tables)
            except Exception:
                logger.exception("registry migration: failed to copy {}", legacy)
                continue
            report[str(legacy)] = copied
            try:
                backup = _backup_path(legacy)
                legacy.replace(backup)
                for suffix in ("-wal", "-shm"):
                    sidecar = Path(f"{legacy}{suffix}")
                    if sidecar.exists():
                        sidecar.replace(Path(f"{backup}{suffix}"))
                logger.info("registry migration: {} -> {} ({})", legacy, registry, copied)
            except OSError:
                logger.exception("registry migration: copied but could not rename {}", legacy)
    finally:
        conn.close()
    return report
