"""In-memory repositories with nested rollback (prototype ``src/core/memory.ts``)."""

from __future__ import annotations

from collections.abc import Callable
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime, UTC
from typing import Any

from superagent.core.ids import thread_key
from superagent.core.model import (
    Decision,
    Effect,
    Receipt,
    SourceRef,
    Ticket,
    Thread,
    Trigger,
    effect_from_dict,
    to_dict,
)

_OUTBOX_FAILURE_LIMIT = 3


class MemoryRepo[T]:
    """Insertion-ordered dict-backed repository."""

    def __init__(self) -> None:
        self._items: dict[str, T] = {}

    def get(self, id_: str) -> T | None:
        """Return an item by id."""
        return self._items.get(id_)

    def exists(self, id_: str) -> bool:
        """Return whether an id is present."""
        return id_ in self._items

    def list(self) -> list[T]:
        """Return all items in insertion order."""
        return list(self._items.values())

    def _put(self, id_: str, item: T) -> None:
        self._items[id_] = deepcopy(item)


class MemoryTriggerRepo(MemoryRepo[Trigger]):
    """Trigger repository."""

    def save(self, trigger: Trigger) -> None:
        """Upsert a Trigger."""
        self._put(trigger.id, trigger)


class MemoryTriggerThreadRepo:
    """Trigger↔Thread association store."""

    def __init__(self) -> None:
        self._items: dict[str, dict[str, Any]] = {}

    @staticmethod
    def association_id(trigger_id: str, ref: SourceRef) -> str:
        """Return the composite association key."""
        return f"{trigger_id}\x1f{thread_key(ref)}"

    def save(self, trigger_id: str, ref: SourceRef, at: str, human: bool) -> None:
        """Upsert an association."""
        self._items[self.association_id(trigger_id, ref)] = {
            "trigger_id": trigger_id,
            "source": ref.source,
            "thread_id": ref.id,
            "at": at,
            "human": human,
        }

    def list_for_trigger(self, trigger_id: str) -> list[SourceRef]:
        """Return the Trigger's Threads."""
        return [
            SourceRef(source=item["source"], id=item["thread_id"])
            for item in self._items.values()
            if item["trigger_id"] == trigger_id
        ]


class MemoryThreadRepo(MemoryRepo[Thread]):
    """Thread repository keyed by canonical thread key."""

    def save(self, thread: Thread) -> None:
        """Upsert a Thread."""
        self._put(thread.key, thread)


class MemoryTicketRepo(MemoryRepo[Ticket]):
    """Ticket repository with panel list helpers."""

    def save(self, ticket: Ticket) -> None:
        """Upsert a Ticket."""
        self._put(ticket.id, ticket)

    def list_open(self) -> list[Ticket]:
        """Return open Tickets in insertion order."""
        return [ticket for ticket in self.list() if ticket.is_open]

    def list_active(self) -> list[Ticket]:
        """Return active Tickets in insertion order."""
        return [ticket for ticket in self.list() if ticket.is_active]


class MemoryDecisionRepo(MemoryRepo[Decision]):
    """Decision repository."""

    def save(self, decision: Decision) -> None:
        """Upsert a Decision."""
        self._put(decision.id, decision)

    def list_for_thread(self, thread: str) -> list[Decision]:
        """Return Decisions for a canonical thread key."""
        return [item for item in self.list() if thread_key(item.thread) == thread]


class MemoryEffectRepo(MemoryRepo[Effect]):
    """Effect repository keyed by ``(kind, key)``."""

    @staticmethod
    def effect_id(kind: str, key: str) -> str:
        """Return the composite in-memory key."""
        return f"{kind}\x1f{key}"

    def save(self, effect: Effect) -> None:
        """Upsert an Effect."""
        self._put(self.effect_id(effect.kind, effect.key), effect)

    def get_pair(self, kind: str, key: str) -> Effect | None:
        """Return an Effect by its natural key."""
        return self.get(self.effect_id(kind, key))

    def due_rows(self, now: str | None = None) -> list[Effect]:
        """Return pending Effects whose due time has arrived."""
        rows = [
            item
            for item in self.list()
            if item.status == "pending"
            and (not item.due_at or (now is not None and item.due_at <= now))
        ]
        return rows

    def set_done(self, kind: str, key: str) -> None:
        """Mark an Effect settled."""
        effect = self.get_pair(kind, key)
        if effect is None:
            return
        effect.status = "done"
        effect.due_at = None
        effect.last_error = None
        self.save(effect)

    def reschedule(self, kind: str, key: str, due_at: str) -> None:
        """Move an Effect's next attempt."""
        effect = self.get_pair(kind, key)
        if effect is None:
            return
        effect.status = "pending"
        effect.due_at = due_at
        self.save(effect)

    def bump_attempts(self, kind: str, key: str, error: str) -> int:
        """Record a failed attempt and return the new count."""
        effect = self.get_pair(kind, key)
        if effect is None:
            return 1
        effect.attempts += 1
        effect.last_error = error
        self.save(effect)
        return effect.attempts


class MemoryReceiptRepo(MemoryRepo[Receipt]):
    """Outbox receipt repository."""

    def save(self, receipt: Receipt) -> None:
        """Upsert a Receipt."""
        self._put(receipt.id, receipt)

    def list_for_ticket(self, ticket_id: str) -> list[Receipt]:
        """Return Receipts for one Ticket."""
        return [item for item in self.list() if item.ticket_id == ticket_id]

    def set_sent(self, receipt_id: str) -> None:
        """Mark a Receipt as delivered."""
        receipt = self.get(receipt_id)
        if receipt is not None:
            receipt.status = "sent"
            receipt.last_error = None
            self.save(receipt)

    def set_dropped(self, receipt_id: str, reason: str) -> None:
        """Mark a Receipt as permanently undeliverable."""
        receipt = self.get(receipt_id)
        if receipt is not None:
            receipt.status = "dropped"
            receipt.dropped = {
                "at": datetime.now(UTC).isoformat(),
                "reason": reason,
            }
            self.save(receipt)

    def behind(self, receipt: Receipt) -> bool:
        """Return whether an earlier same-channel Receipt is still in flight."""
        channel = to_dict(receipt.channel)
        for item in self.list():
            if item is receipt or item.id == receipt.id:
                break
            if (
                item.ticket_id == receipt.ticket_id
                and to_dict(item.channel) == channel
                and item.status == "pending"
                and item.attempts < _OUTBOX_FAILURE_LIMIT
            ):
                return True
        return False


@dataclass
class _Frame:
    """One nested transaction snapshot."""

    snapshot: dict[str, dict[str, Any]]
    callbacks: list[Callable[[], None]] = field(default_factory=list)


class MemoryAtomic:
    """Re-entrant transaction context with deep-copy rollback."""

    def __init__(self, repos: MemoryRepositories) -> None:
        self._repos = repos
        self._stack: list[_Frame] = []

    def __enter__(self) -> MemoryAtomic:
        """Snapshot every repository."""
        self._stack.append(_Frame(self._repos._snapshot()))
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: Any,
    ) -> None:
        """Commit to the parent frame or restore the snapshot."""
        del exc, traceback
        if not self._stack:
            return
        frame = self._stack.pop()
        if exc_type is not None:
            self._repos._restore(frame.snapshot)
            return
        if self._stack:
            self._stack[-1].callbacks.extend(frame.callbacks)
            return
        for callback in frame.callbacks:
            callback()

    def after_commit(self, callback: Callable[[], None]) -> None:
        """Register a callback for the outermost successful commit."""
        if not self._stack:
            raise RuntimeError("after_commit requires an active transaction")
        self._stack[-1].callbacks.append(callback)


class MemoryRepositories:
    """SQLite Store mirror used by pure domain tests."""

    def __init__(self) -> None:
        self.triggers = MemoryTriggerRepo()
        self.trigger_threads = MemoryTriggerThreadRepo()
        self.threads = MemoryThreadRepo()
        self.tickets = MemoryTicketRepo()
        self.decisions = MemoryDecisionRepo()
        self.effects = MemoryEffectRepo()
        self.outbox = MemoryReceiptRepo()
        self._atomic = MemoryAtomic(self)

    @property
    def atomic(self) -> MemoryAtomic:
        """Return the re-entrant transaction manager."""
        return self._atomic

    def _snapshot(self) -> dict[str, dict[str, Any]]:
        return {
            name: deepcopy(getattr(self, name)._items)
            for name in (
                "triggers",
                "trigger_threads",
                "threads",
                "tickets",
                "decisions",
                "effects",
                "outbox",
            )
        }

    def _restore(self, snapshot: dict[str, dict[str, Any]]) -> None:
        for name, items in snapshot.items():
            getattr(self, name)._items = deepcopy(items)


__all__ = [
    "MemoryAtomic",
    "MemoryDecisionRepo",
    "MemoryEffectRepo",
    "MemoryReceiptRepo",
    "MemoryRepo",
    "MemoryThreadRepo",
    "MemoryTicketRepo",
    "MemoryTriggerRepo",
    "MemoryTriggerThreadRepo",
    "effect_from_dict",
]
