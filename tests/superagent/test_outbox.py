"""Outbox receipt and reply tests."""

from __future__ import annotations

import pytest

from superagent.app.effects import Effects, Later
from superagent.app.outbox import Outbox
from superagent.app.store import Store
from superagent.infra.health import HealthBoard
from tests.superagent.conftest import make_receipt


class Sources:
    """Fake Source transport."""

    def __init__(self, allowed: bool = True, result: str = "sent") -> None:
        self.allowed = allowed
        self.result = result
        self.sent: list[str] = []

    def channel_allowed(self, channel) -> bool:
        return self.allowed

    def send(self, receipt) -> str:
        self.sent.append(receipt.id)
        if self.result == "raise":
            raise RuntimeError("network down")
        return self.result


def build(tmp_path):
    store = Store(tmp_path / "orchestrator.sqlite", tmp_path)
    board = HealthBoard()
    effects = Effects(store, board)
    sources = Sources()
    outbox = Outbox(store, sources, board, effects)
    return store, effects, sources, outbox


@pytest.mark.asyncio
async def test_send_is_idempotent_and_enqueues_reply(tmp_path) -> None:
    store, effects, sources, outbox = build(tmp_path)
    message = make_receipt()
    outbox.send(message)
    outbox.send(message)
    assert len(store.receipts.list()) == 1
    assert effects.count("reply") == 1
    assert outbox.status(message.id) == "pending"
    await outbox.handle_reply({"receipt_id": message.id})
    assert outbox.status(message.id) == "sent"
    assert sources.sent == [message.id]
    store.close()


@pytest.mark.asyncio
async def test_gone_and_withdraw_drop_receipts(tmp_path) -> None:
    store, effects, sources, outbox = build(tmp_path)
    gone = make_receipt("msg-gone")
    store.receipts.save(gone)
    sources.result = "gone"
    await outbox.handle_reply({"receipt_id": gone.id})
    assert outbox.status(gone.id) == "dropped"

    ignored = make_receipt("msg-ignored")
    outbox.send(ignored)
    sources.allowed = False
    await outbox.handle_reply({"receipt_id": ignored.id})
    assert outbox.status(ignored.id) == "dropped"
    store.close()


@pytest.mark.asyncio
async def test_ordering_waits_for_pending_predecessor(tmp_path) -> None:
    store, effects, sources, outbox = build(tmp_path)
    first = make_receipt("msg-1")
    second = make_receipt("msg-2")
    outbox.send(first)
    outbox.send(second)
    assert outbox.behind(second) is True
    with pytest.raises(Later):
        await outbox.handle_reply({"receipt_id": second.id})
    outbox.set_sent(first.id)
    assert outbox.behind(second) is False
    await outbox.handle_reply({"receipt_id": second.id})
    assert sources.sent == ["msg-2"]
    store.close()


@pytest.mark.asyncio
async def test_transient_send_failure_propagates(tmp_path) -> None:
    store, effects, sources, outbox = build(tmp_path)
    sources.result = "raise"
    message = make_receipt()
    outbox.send(message)
    with pytest.raises(RuntimeError, match="network down"):
        await outbox.handle_reply({"receipt_id": message.id})
    assert outbox.status(message.id) == "pending"
    store.close()
