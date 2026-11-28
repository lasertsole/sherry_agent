"""Outbox receipts and the reply Effect handler (prototype ``src/app/outbox.ts``)."""

from __future__ import annotations

from collections.abc import Awaitable
from datetime import datetime, UTC
from inspect import isawaitable
from typing import Any, Protocol

from superagent.app.effects import Effects, Later
from superagent.app.store import Store
from superagent.core.model import ChannelAddress, Receipt
from superagent.infra.health import HealthBoard


class OutboxSources(Protocol):
    """Narrow Source surface needed to send a receipt."""

    def channel_allowed(self, channel: ChannelAddress) -> bool:
        """Return whether the destination is still listened to."""
        ...

    def send(self, receipt: Receipt) -> str | Awaitable[str]:
        """Return ``sent``, ``gone`` or raise for a transient failure."""
        ...


class Outbox:
    """Single writer of Receipts and Engine Outbox port."""

    def __init__(
        self,
        store: Store,
        sources: OutboxSources,
        board: HealthBoard,
        effects: Effects | None = None,
    ) -> None:
        self.store = store
        self.sources = sources
        self.board = board
        self.effects = effects
        self.board.claim("reply", "reply")

    def send(self, message: Receipt) -> None:
        """Persist a Receipt idempotently and enqueue its reply Effect."""
        if self.store.receipts.get(message.id) is not None:
            return
        self.store.receipts.save(message)
        if self.effects is not None:
            self.effects.enqueue("reply", message.id, {"receipt_id": message.id})

    def status(self, receipt_id: str) -> str | None:
        """Return a Receipt's current status."""
        receipt = self.store.receipts.get(receipt_id)
        return receipt.status if receipt else None

    def withdraw(self, receipt_id: str) -> None:
        """Drop a Receipt because the Source no longer listens."""
        self.store.receipts.set_dropped(receipt_id, "channelIgnored")

    def behind(self, receipt: Receipt) -> bool:
        """Return whether an earlier same-channel message blocks this one."""
        return self.store.receipts.behind(receipt)

    def set_sent(self, receipt_id: str) -> None:
        """Mark a Receipt delivered."""
        self.store.receipts.set_sent(receipt_id)

    def set_dropped(self, receipt_id: str, reason: str) -> None:
        """Mark a Receipt dropped with a stable reason."""
        self.store.receipts.set_dropped(receipt_id, reason)

    async def handle_reply(self, payload: dict[str, Any]) -> None:
        """Deliver one Receipt; raise on transient failures."""
        receipt_id = str(payload.get("receipt_id", ""))
        receipt = self.store.receipts.get(receipt_id)
        if receipt is None or receipt.status in ("sent", "dropped"):
            return
        if self.behind(receipt):
            raise Later()
        if not self.sources.channel_allowed(receipt.channel):
            self.withdraw(receipt_id)
            return
        result = self.sources.send(receipt)
        if isawaitable(result):
            result = await result
        if result == "sent":
            self.set_sent(receipt_id)
            self.board.clear("reply")
        elif result == "gone":
            self.set_dropped(receipt_id, "channelGone")
        else:
            raise ValueError(f"unknown send result: {result}")

    def dropped_at(self) -> str:
        """Return the current UTC timestamp for admin operations."""
        return datetime.now(UTC).isoformat()


__all__ = ["Outbox", "OutboxSources"]
