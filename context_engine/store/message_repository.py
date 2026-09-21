"""Repository for the ``messages``-table read queries (audit #8).

The two history readers in :mod:`context_engine.store.core`
(``get_turns_by_turn_num_scope`` and ``get_history_by_turn_page``) built the
same turn-range SELECT with the same eligibility / compaction filters, and
``get_max_turn_num`` / ``get_message_by_id`` embedded their own message reads.
This repository is the single home for those reads.

The connection is injected as a provider so this module performs no import-time
I/O and the store module keeps its lazy ``_shared_db()`` accessor — including
its test seam that injects a connection by patching ``core._db``.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable


class MessageRepository:
    """Turn-range and identity reads over the ``messages`` table."""

    def __init__(self, db_provider: Callable[[], sqlite3.Connection]) -> None:
        self._db_provider = db_provider

    def max_turn_num(self, session_id: str) -> int:
        """Maximum ``turn_num`` of a session; ``0`` when it has no messages."""
        row = (
            self._db_provider()
            .execute("SELECT MAX(turn_num) FROM messages WHERE session_id = ?", (session_id,))
            .fetchone()
        )
        return row[0] if row and row[0] is not None else 0

    def fetch_turn_range(
        self,
        session_id: str,
        min_turn_num: int,
        max_turn_num: int,
        *,
        only_eligible: bool,
        include_compacted: bool,
    ) -> list[sqlite3.Row]:
        """Rows of one session whose ``turn_num`` falls in the inclusive range.

        Ordered by ``turn_num DESC, id ASC``. Compacted rows are excluded
        unless ``include_compacted``; context-ineligible rows are excluded
        unless ``only_eligible`` is False.
        """
        compacted_filter = "" if include_compacted else " AND compacted = 0"
        eligible_filter = " AND context_eligible = 1" if only_eligible else ""
        return (
            self._db_provider()
            .execute(
                "SELECT * FROM messages "
                "WHERE session_id = ? AND turn_num >= ? AND turn_num <= ?"
                f"{compacted_filter}{eligible_filter} "
                "ORDER BY turn_num DESC, id ASC",
                (session_id, min_turn_num, max_turn_num),
            )
            .fetchall()
        )

    def get_by_id(self, message_id: int) -> sqlite3.Row | None:
        """One message row by primary key; ids are globally unique (AUTOINCREMENT)."""
        return (
            self._db_provider()
            .execute("SELECT * FROM messages WHERE id = ?", (message_id,))
            .fetchone()
        )
