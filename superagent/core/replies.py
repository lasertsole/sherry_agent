"""Channel reply rules (prototype ``src/core/replies.py``)."""

from __future__ import annotations

from typing import Any

from superagent.core.errors import UserError
from superagent.core.ids import key
from superagent.core.model import Channel, Input, Receipt
from superagent.core.part import EnginePart
from superagent.core.stage import ticket_stage
from superagent.limits import MAX_ACK_TEXT


class Replies(EnginePart):
    """Writes Channel messages through the Outbox port."""

    def reply(
        self,
        ticket_id: str,
        purpose: Any,
        text: str,
        channel: Any | None = None,
    ) -> Receipt:
        """Persist a reply Receipt in the current transaction."""
        engine = self.engine
        ticket = engine.repo.tickets.get(ticket_id)
        if ticket is None:
            raise UserError("not_found", {"ticket_id": ticket_id})
        try:
            reason = Channel(purpose) if not isinstance(purpose, Channel) else purpose
        except ValueError as exc:
            raise UserError("invalid_value", {"purpose": purpose}) from exc
        destination = channel or (ticket.channels[0] if ticket.channels else None)
        if destination is None:
            raise UserError("channel_ignored", {"ticket_id": ticket_id})
        value = text[:MAX_ACK_TEXT] if reason == Channel.ACKNOWLEDGEMENT else text
        receipt = Receipt(
            id=key(ticket_id, reason.value, engine.now()),
            ticket_id=ticket_id,
            channel=destination,
            purpose=reason,
            text=value,
        )
        with engine.repo.atomic as tx:
            engine.outbox.send(receipt)
            tx.after_commit(lambda: None)
        return receipt

    def note(self, ticket_id: str, text: str) -> None:
        """Add a context-only note to a Ticket."""
        engine = self.engine
        ticket = engine.repo.tickets.get(ticket_id)
        if ticket is None:
            raise UserError("not_found", {"ticket_id": ticket_id})
        ticket.inputs.append(
            Input(
                id=key(ticket_id, "note", engine.now()),
                ticket_id=ticket_id,
                kind="text",
                trigger_id=None,
                text=text,
                delivered=True,
            )
        )
        engine.repo.tickets.save(ticket)

    def request_status(self, ticket_id: str) -> bool:
        """Send the current stage back to the requester."""
        ticket = self.engine.repo.tickets.get(ticket_id)
        if ticket is None:
            raise UserError("not_found", {"ticket_id": ticket_id})
        stage = ticket_stage(ticket)
        self.reply(ticket_id, Channel.ANSWER, f"Ticket is {stage.value if stage else 'ended'}")
        return True


__all__ = ["Replies"]
