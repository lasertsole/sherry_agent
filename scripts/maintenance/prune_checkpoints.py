#!/usr/bin/env python3
"""One-shot checkpoint DB compaction: keep the latest top-level row per thread.

This is the historical migration companion to the per-turn/per-child cleanup.
For every thread it keeps the newest ``checkpoint_ns=''`` checkpoint (the row
``aget_state`` reads), or the newest row if the thread has no top-level row,
and deletes every other checkpoint. Writes are kept only for the selected
checkpoint.

Usage:
    uv run --no-sync python scripts/maintenance/prune_checkpoints.py --dry-run
    uv run --no-sync python scripts/maintenance/prune_checkpoints.py --apply --vacuum

Run with the server stopped. The default is ``--dry-run``.
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))


def _default_db() -> Path:
    from config.path import SRC_DIR

    return (SRC_DIR / "checkpoints" / "sqlite.db").resolve()


def _counts(conn: sqlite3.Connection) -> dict[str, int]:
    return {
        "checkpoints": conn.execute("SELECT count(*) FROM checkpoints").fetchone()[0],
        "writes": conn.execute("SELECT count(*) FROM writes").fetchone()[0],
        "threads": conn.execute("SELECT count(DISTINCT thread_id) FROM checkpoints").fetchone()[0],
    }


def _keep_rows(conn: sqlite3.Connection) -> list[tuple[int, str, str, str]]:
    return conn.execute(
        "SELECT c.rowid, c.thread_id, c.checkpoint_ns, c.checkpoint_id "
        "FROM checkpoints c WHERE c.rowid = ("
        "  SELECT c2.rowid FROM checkpoints c2 WHERE c2.thread_id = c.thread_id "
        "  ORDER BY (c2.checkpoint_ns = '') DESC, c2.rowid DESC LIMIT 1"
        ")"
    ).fetchall()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=None, help="checkpoint sqlite path")
    parser.add_argument("--apply", action="store_true", help="write changes (default: dry run)")
    parser.add_argument("--dry-run", action="store_true", help="explicit dry run (default)")
    parser.add_argument("--vacuum", action="store_true", help="VACUUM after applying")
    args = parser.parse_args(argv)

    db = (args.db or _default_db()).resolve()
    if not db.exists():
        print(f"checkpoint db not found: {db}")
        return 1

    conn = sqlite3.connect(db)
    try:
        before = _counts(conn)
        keep = _keep_rows(conn)
        print(f"db: {db}")
        print(f"before: {before}")
        print(f"threads to keep: {len(keep)}")
        if not args.apply or args.dry_run:
            print("dry-run: no changes (pass --apply to delete)")
            return 0

        placeholders = ",".join("?" for _ in keep)
        deleted_checkpoints = 0
        deleted_writes = 0
        if keep:
            rowids = tuple(row[0] for row in keep)
            deleted_checkpoints = conn.execute(
                f"DELETE FROM checkpoints WHERE rowid NOT IN ({placeholders})", rowids
            ).rowcount
            conn.execute(
                "CREATE TEMP TABLE _keep_threads ("
                "thread_id TEXT, checkpoint_ns TEXT, checkpoint_id TEXT, PRIMARY KEY("
                "thread_id, checkpoint_ns, checkpoint_id))"
            )
            conn.executemany(
                "INSERT INTO _keep_threads VALUES (?, ?, ?)",
                [(row[1], row[2], row[3]) for row in keep],
            )
            deleted_writes = conn.execute(
                "DELETE FROM writes WHERE NOT EXISTS ("
                "SELECT 1 FROM _keep_threads k WHERE k.thread_id = writes.thread_id "
                "AND k.checkpoint_ns = writes.checkpoint_ns "
                "AND k.checkpoint_id = writes.checkpoint_id)"
            ).rowcount
        else:
            deleted_checkpoints = conn.execute("DELETE FROM checkpoints").rowcount
            deleted_writes = conn.execute("DELETE FROM writes").rowcount
        conn.commit()
        print(f"deleted checkpoints: {deleted_checkpoints}, writes: {deleted_writes}")
        print(f"after: {_counts(conn)}")
        if args.vacuum:
            conn.execute("VACUUM")
            print("vacuumed")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
