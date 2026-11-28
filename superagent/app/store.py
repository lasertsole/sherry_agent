"""SQLite Store and repositories (prototype ``src/app/store.ts``)."""

from __future__ import annotations

import fcntl
import json
import os
import sqlite3
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass, field
from datetime import datetime, UTC
from pathlib import Path
from typing import Any

from superagent.app.migration import SCHEMA_VERSION, ensure_schema_11
from superagent.app.store_read import (
    EVAL_META_KEY,
    StoreRead,
    decode_decision,
    decode_effect,
    decode_receipt,
    decode_thread,
    decode_ticket,
    decode_trigger,
)
from superagent.core.ids import thread_key as canonical_thread_key
from superagent.core.model import (
    Decision,
    Effect,
    Receipt,
    SourceRef,
    Ticket,
    Thread,
    Trigger,
    to_dict,
)
from superagent.kv import KV, KVEntry
from superagent.limits import OUTBOX_FAILURE_LIMIT


class StoreLockedError(RuntimeError):
    """Raised when another process already owns the state directory."""


def _dump(value: Any) -> str:
    """Serialize a domain value deterministically."""
    return json.dumps(to_dict(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"))


@dataclass
class _TxFrame:
    """One SQLite transaction frame."""

    name: str
    callbacks: list[Callable[[], None]] = field(default_factory=list)


class _Tx:
    """Handle used to register commit callbacks."""

    def __init__(self, frame: _TxFrame) -> None:
        self._frame = frame

    def after_commit(self, callback: Callable[[], None]) -> None:
        """Run ``callback`` only after the outermost transaction commits."""
        self._frame.callbacks.append(callback)


class _FileLock:
    """Non-blocking advisory lock held for the Store lifetime."""

    def __init__(self, lock_dir: Path) -> None:
        lock_dir.mkdir(parents=True, exist_ok=True)
        self._fd = os.open(lock_dir / "lock", os.O_CREAT | os.O_RDWR | os.O_CLOEXEC, 0o600)
        try:
            fcntl.flock(self._fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            os.close(self._fd)
            self._fd = -1
            raise StoreLockedError(f"state directory is already in use: {lock_dir}") from exc
        os.ftruncate(self._fd, 0)
        os.write(self._fd, str(os.getpid()).encode("ascii"))

    def release(self) -> None:
        """Release and close the lock file."""
        if self._fd < 0:
            return
        fcntl.flock(self._fd, fcntl.LOCK_UN)
        os.close(self._fd)
        self._fd = -1


class TriggerRepo:
    """Trigger CRUD."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def get(self, trigger_id: str) -> Trigger | None:
        """Return a Trigger by id."""
        row = self._conn.execute("SELECT * FROM trigger WHERE id = ?", (trigger_id,)).fetchone()
        return decode_trigger(self._conn, row) if row is not None else None

    def exists(self, trigger_id: str) -> bool:
        """Return whether a Trigger id exists."""
        return (
            self._conn.execute("SELECT 1 FROM trigger WHERE id = ?", (trigger_id,)).fetchone()
            is not None
        )

    def save(self, trigger: Trigger) -> None:
        """Upsert a Trigger."""
        self._conn.execute(
            """
            INSERT INTO trigger(
              id, source, actor_json, human, addressed, at, text, channel_json, url, meta_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
              source = excluded.source, actor_json = excluded.actor_json,
              human = excluded.human, addressed = excluded.addressed,
              at = excluded.at, text = excluded.text, channel_json = excluded.channel_json,
              url = excluded.url, meta_json = excluded.meta_json
            """,
            (
                trigger.id,
                trigger.source,
                _dump(trigger.actor),
                int(trigger.human),
                trigger.addressed.value if trigger.addressed else None,
                trigger.at,
                trigger.text,
                _dump(trigger.channel) if trigger.channel else None,
                trigger.url,
                _dump(trigger.meta),
            ),
        )

    def list(self) -> list[Trigger]:
        """Return Triggers in insertion order."""
        rows = self._conn.execute("SELECT * FROM trigger ORDER BY rowid").fetchall()
        return [decode_trigger(self._conn, row) for row in rows]


class TriggerThreadRepo:
    """Trigger↔Thread association CRUD."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def save(self, trigger_id: str, ref: SourceRef, at: str, human: bool) -> None:
        """Upsert an association."""
        self._conn.execute(
            """
            INSERT INTO trigger_thread(trigger_id, thread_key, source, thread_id, at, human)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(trigger_id, thread_key) DO UPDATE SET
              source = excluded.source, thread_id = excluded.thread_id,
              at = excluded.at, human = excluded.human
            """,
            (
                trigger_id,
                canonical_thread_key(ref),
                ref.source,
                ref.id,
                at,
                int(human),
            ),
        )

    def list_for_trigger(self, trigger_id: str) -> list[SourceRef]:
        """Return the Trigger's Threads."""
        rows = self._conn.execute(
            "SELECT source, thread_id FROM trigger_thread WHERE trigger_id = ? "
            "ORDER BY at, thread_key",
            (trigger_id,),
        ).fetchall()
        return [SourceRef(source=row["source"], id=row["thread_id"]) for row in rows]


class ThreadRepo:
    """Thread CRUD."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def get(self, key: str) -> Thread | None:
        """Return a Thread by canonical key."""
        row = self._conn.execute("SELECT * FROM thread WHERE thread_key = ?", (key,)).fetchone()
        return decode_thread(row) if row is not None else None

    def save(self, thread: Thread) -> None:
        """Upsert a Thread and maintain its explicit flags."""
        meta = dict(thread.meta) if thread.meta else {}
        if thread.eval is not None:
            meta[EVAL_META_KEY] = to_dict(thread.eval)
        else:
            meta.pop(EVAL_META_KEY, None)
        self._conn.execute(
            """
            INSERT INTO thread(
              thread_key, source, thread_id, subscriptions_json, context_json,
              queued_trigger_ids_json, eval_id, expired_count, meta_json,
              has_queued, has_eval, is_subscribed
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(thread_key) DO UPDATE SET
              source = excluded.source, thread_id = excluded.thread_id,
              subscriptions_json = excluded.subscriptions_json,
              context_json = excluded.context_json,
              queued_trigger_ids_json = excluded.queued_trigger_ids_json,
              eval_id = excluded.eval_id, expired_count = excluded.expired_count,
              meta_json = excluded.meta_json, has_queued = excluded.has_queued,
              has_eval = excluded.has_eval, is_subscribed = excluded.is_subscribed
            """,
            (
                thread.key,
                thread.source,
                thread.id,
                _dump(thread.subscriptions),
                _dump(thread.context) if thread.context is not None else None,
                _dump(thread.queued_trigger_ids),
                thread.eval.id if thread.eval is not None else None,
                thread.expired_count,
                _dump(meta) if meta else None,
                int(thread.has_queued),
                int(thread.has_eval),
                int(thread.is_subscribed),
            ),
        )

    def list(self) -> list[Thread]:
        """Return Threads in insertion order."""
        return [
            decode_thread(row) for row in self._conn.execute("SELECT * FROM thread ORDER BY rowid")
        ]


class TicketRepo:
    """Ticket CRUD with explicit state columns."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def get(self, ticket_id: str) -> Ticket | None:
        """Return a Ticket by id."""
        row = self._conn.execute("SELECT * FROM ticket WHERE id = ?", (ticket_id,)).fetchone()
        return decode_ticket(row) if row is not None else None

    def save(self, ticket: Ticket) -> None:
        """Upsert a Ticket and maintain its explicit flags."""
        self._conn.execute(
            """
            INSERT INTO ticket(
              id, origin_json, mandate_json, execution_json, channels_json, inputs_json,
              follows_json, assignments_json, latest_report_json, ended_json,
              ended_at, latest_assignment_status, has_undelivered_input
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
              origin_json = excluded.origin_json, mandate_json = excluded.mandate_json,
              execution_json = excluded.execution_json, channels_json = excluded.channels_json,
              inputs_json = excluded.inputs_json, follows_json = excluded.follows_json,
              assignments_json = excluded.assignments_json,
              latest_report_json = excluded.latest_report_json,
              ended_json = excluded.ended_json, ended_at = excluded.ended_at,
              latest_assignment_status = excluded.latest_assignment_status,
              has_undelivered_input = excluded.has_undelivered_input
            """,
            (
                ticket.id,
                _dump(ticket.origin),
                _dump(ticket.mandate),
                _dump(ticket.execution),
                _dump(ticket.channels),
                _dump(ticket.inputs),
                _dump(ticket.follows),
                _dump(ticket.assignments),
                _dump(ticket.latest_report) if ticket.latest_report is not None else None,
                _dump(ticket.ended) if ticket.ended is not None else None,
                ticket.ended_at,
                ticket.latest_assignment_status,
                int(ticket.has_undelivered_input),
            ),
        )

    def list(self) -> list[Ticket]:
        """Return Tickets in insertion order."""
        return [
            decode_ticket(row) for row in self._conn.execute("SELECT * FROM ticket ORDER BY rowid")
        ]

    def list_open(self) -> list[Ticket]:
        """Return open Tickets via the generated index."""
        rows = self._conn.execute("SELECT * FROM ticket WHERE is_open = 1 ORDER BY rowid")
        return [decode_ticket(row) for row in rows]

    def list_active(self) -> list[Ticket]:
        """Return active Tickets via the generated index."""
        rows = self._conn.execute("SELECT * FROM ticket WHERE is_active = 1 ORDER BY rowid")
        return [decode_ticket(row) for row in rows]


class DecisionRepo:
    """Decision CRUD."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def get(self, decision_id: str) -> Decision | None:
        """Return a Decision by id."""
        row = self._conn.execute("SELECT * FROM decision WHERE id = ?", (decision_id,)).fetchone()
        return decode_decision(row) if row is not None else None

    def save(self, decision: Decision) -> None:
        """Upsert a Decision."""
        reason_envelope = {
            "__source": decision.thread.source,
            "__thread_id": decision.thread.id,
            "__reason": to_dict(decision.reason),
        }
        self._conn.execute(
            """
            INSERT INTO decision(id, thread_key, outcome, reason_json, at, trigger_ids_json)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
              thread_key = excluded.thread_key, outcome = excluded.outcome,
              reason_json = excluded.reason_json, at = excluded.at,
              trigger_ids_json = excluded.trigger_ids_json
            """,
            (
                decision.id,
                canonical_thread_key(decision.thread),
                decision.outcome.value,
                _dump(reason_envelope),
                decision.at,
                _dump(decision.trigger_ids),
            ),
        )

    def list(self, thread: str | None = None) -> list[Decision]:
        """Return Decisions in insertion order, optionally for one Thread."""
        if thread is None:
            rows = self._conn.execute("SELECT * FROM decision ORDER BY rowid")
        else:
            rows = self._conn.execute(
                "SELECT * FROM decision WHERE thread_key = ? ORDER BY rowid", (thread,)
            )
        return [decode_decision(row) for row in rows]


class EffectRepo:
    """Durable effect queue CRUD."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def get(self, kind: str, key: str) -> Effect | None:
        """Return an Effect by natural key."""
        row = self._conn.execute(
            "SELECT * FROM effect WHERE kind = ? AND key = ?", (kind, key)
        ).fetchone()
        return decode_effect(row) if row is not None else None

    def save(self, effect: Effect) -> None:
        """Upsert an Effect without replacing its sequence number."""
        self._conn.execute(
            """
            INSERT INTO effect(kind, key, payload_json, status, attempts, last_error, due_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(kind, key) DO UPDATE SET
              payload_json = excluded.payload_json, status = excluded.status,
              attempts = excluded.attempts, last_error = excluded.last_error,
              due_at = excluded.due_at
            """,
            (
                effect.kind,
                effect.key,
                _dump(effect.payload),
                effect.status,
                effect.attempts,
                effect.last_error,
                effect.due_at,
            ),
        )

    def list(self) -> list[Effect]:
        """Return Effects by sequence number."""
        return [
            decode_effect(row) for row in self._conn.execute("SELECT * FROM effect ORDER BY seq")
        ]

    def due_rows(self, now: str | None = None) -> list[Effect]:
        """Return pending Effects whose due time has arrived."""
        if now is None:
            rows = self._conn.execute(
                "SELECT * FROM effect WHERE status = 'pending' "
                "AND (due_at IS NULL OR due_at = '') ORDER BY seq"
            )
        else:
            rows = self._conn.execute(
                "SELECT * FROM effect WHERE status = 'pending' "
                "AND (due_at IS NULL OR due_at = '' OR due_at <= ?) ORDER BY seq",
                (now,),
            )
        return [decode_effect(row) for row in rows]

    def set_done(self, kind: str, key: str) -> None:
        """Mark an Effect settled while retaining its row."""
        self._conn.execute(
            "UPDATE effect SET status = 'done', due_at = NULL, last_error = NULL "
            "WHERE kind = ? AND key = ?",
            (kind, key),
        )

    def reschedule(self, kind: str, key: str, due_at: str) -> None:
        """Move an Effect's next attempt."""
        self._conn.execute(
            "UPDATE effect SET due_at = ?, status = 'pending' WHERE kind = ? AND key = ?",
            (due_at, kind, key),
        )

    def bump_attempts(self, kind: str, key: str, error: str) -> int:
        """Record a failed attempt and return the new count."""
        row = self._conn.execute(
            "SELECT attempts FROM effect WHERE kind = ? AND key = ?", (kind, key)
        ).fetchone()
        attempts = int(row["attempts"]) + 1 if row is not None else 1
        self._conn.execute(
            "UPDATE effect SET attempts = ?, last_error = ? WHERE kind = ? AND key = ?",
            (attempts, error, kind, key),
        )
        return attempts


class ReceiptRepo:
    """Outbox receipt CRUD."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def get(self, receipt_id: str) -> Receipt | None:
        """Return a Receipt by id."""
        row = self._conn.execute("SELECT * FROM outbox WHERE id = ?", (receipt_id,)).fetchone()
        return decode_receipt(row) if row is not None else None

    def save(self, receipt: Receipt) -> None:
        """Upsert a Receipt."""
        self._conn.execute(
            """
            INSERT INTO outbox(
              id, ticket_id, channel_json, purpose, text, status, attempts,
              last_error, dropped_at, dropped_reason
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
              ticket_id = excluded.ticket_id, channel_json = excluded.channel_json,
              purpose = excluded.purpose, text = excluded.text, status = excluded.status,
              attempts = excluded.attempts, last_error = excluded.last_error,
              dropped_at = excluded.dropped_at, dropped_reason = excluded.dropped_reason
            """,
            (
                receipt.id,
                receipt.ticket_id,
                _dump(receipt.channel),
                receipt.purpose.value,
                receipt.text,
                receipt.status,
                receipt.attempts,
                receipt.last_error,
                receipt.dropped.get("at") if receipt.dropped else None,
                receipt.dropped.get("reason") if receipt.dropped else None,
            ),
        )

    def list(self, ticket_id: str | None = None) -> list[Receipt]:
        """Return Receipts in insertion order."""
        if ticket_id is None:
            rows = self._conn.execute("SELECT * FROM outbox ORDER BY rowid")
        else:
            rows = self._conn.execute(
                "SELECT * FROM outbox WHERE ticket_id = ? ORDER BY rowid", (ticket_id,)
            )
        return [decode_receipt(row) for row in rows]

    def set_sent(self, receipt_id: str) -> None:
        """Mark a Receipt as delivered."""
        self._conn.execute(
            "UPDATE outbox SET status = 'sent', last_error = NULL WHERE id = ?",
            (receipt_id,),
        )

    def set_dropped(self, receipt_id: str, reason: str) -> None:
        """Mark a Receipt as permanently undeliverable."""
        self._conn.execute(
            "UPDATE outbox SET status = 'dropped', dropped_at = ?, dropped_reason = ? WHERE id = ?",
            (datetime.now(UTC).isoformat(), reason, receipt_id),
        )

    def behind(self, receipt: Receipt) -> bool:
        """Return whether an earlier same-channel Receipt is still in flight."""
        row = self._conn.execute("SELECT rowid FROM outbox WHERE id = ?", (receipt.id,)).fetchone()
        if row is None:
            return False
        earlier = self._conn.execute(
            "SELECT 1 FROM outbox WHERE rowid < ? AND ticket_id IS ? AND channel_json = ? "
            "AND status = 'pending' AND attempts < ? LIMIT 1",
            (
                row["rowid"],
                receipt.ticket_id,
                _dump(receipt.channel),
                OUTBOX_FAILURE_LIMIT,
            ),
        ).fetchone()
        return earlier is not None


class Store:
    """Owner of ``orchestrator.sqlite`` and its single state-directory lock."""

    def __init__(self, path: str | Path, lock_dir: str | Path) -> None:
        self._lock = _FileLock(Path(lock_dir))
        db_path = Path(path)
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(db_path), isolation_level=None, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA busy_timeout = 5000")
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._conn.execute("PRAGMA journal_mode = WAL")
        ensure_schema_11(self._conn)
        self._tx_stack: list[_TxFrame] = []
        self._closed = False
        self.triggers = TriggerRepo(self._conn)
        self.trigger_threads = TriggerThreadRepo(self._conn)
        self.threads = ThreadRepo(self._conn)
        self.tickets = TicketRepo(self._conn)
        self.decisions = DecisionRepo(self._conn)
        self.effects = EffectRepo(self._conn)
        self.receipts = ReceiptRepo(self._conn)
        self.read = StoreRead(self._conn)

    @property
    def atomic(self) -> AbstractContextManager[_Tx]:
        """Return a nested transaction context with commit-only callbacks."""
        return self._transaction()

    @contextmanager
    def _transaction(self) -> Iterator[_Tx]:
        """Implement the nested transaction frames."""
        nested = bool(self._tx_stack)
        frame = _TxFrame(f"sp_{len(self._tx_stack)}")
        if nested:
            self._conn.execute(f"SAVEPOINT {frame.name}")
        else:
            self._conn.execute("BEGIN IMMEDIATE")
        self._tx_stack.append(frame)
        try:
            yield _Tx(frame)
        except BaseException:
            self._tx_stack.pop()
            if nested:
                self._conn.execute(f"ROLLBACK TO {frame.name}")
                self._conn.execute(f"RELEASE {frame.name}")
            else:
                self._conn.execute("ROLLBACK")
            raise
        else:
            self._tx_stack.pop()
            if nested:
                self._tx_stack[-1].callbacks.extend(frame.callbacks)
                self._conn.execute(f"RELEASE {frame.name}")
            else:
                self._conn.execute("COMMIT")
                for callback in frame.callbacks:
                    callback()

    def kv_get(self, entry: KVEntry[Any]) -> Any:
        """Read a registered KV entry."""
        return KV.get(self._conn, entry)

    def kv_set(self, entry: KVEntry[Any], value: Any) -> None:
        """Write a registered KV entry."""
        KV.set(self._conn, entry, value)

    def schema_version(self) -> int:
        """Return the on-disk schema version."""
        return int(self._conn.execute("PRAGMA user_version").fetchone()[0])

    def close(self) -> None:
        """Close the connection and release the lock."""
        if self._closed:
            return
        self._conn.close()
        self._lock.release()
        self._closed = True

    def __enter__(self) -> Store:
        """Return this Store as a context manager."""
        return self

    def __exit__(self, *exc: object) -> None:
        """Close on exit."""
        self.close()


__all__ = [
    "SCHEMA_VERSION",
    "DecisionRepo",
    "EffectRepo",
    "ReceiptRepo",
    "Store",
    "StoreLockedError",
    "ThreadRepo",
    "TicketRepo",
    "TriggerRepo",
]
