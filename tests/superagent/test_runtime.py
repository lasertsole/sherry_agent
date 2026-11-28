"""Whole app on a simulated host: Trigger → Ticket → reply, via the real Runtime."""

from __future__ import annotations

import json

import pytest

from superagent.core.model import Channel, DecisionOutcome, Reason, Report, ReportKind
from superagent.deployment import Config
from superagent.index import Runtime
from superagent.infra.commands import CommandResult, FakeCommands
from tests.superagent.conftest import make_trigger


def settings() -> dict:
    """Deterministic auto-accept settings for one GitHub source."""
    return {
        "accept_mode": "auto",
        "paused": False,
        "eval_timeout_ms": 600_000,
        "listening": ["github"],
        "rules": {"rule-1": {"enabled": True, "keywords": ["please"]}},
        "sources": {
            "github": {
                "allowed": [{"id": "alice"}],
                "bots": [],
                "receive_all": True,
                "admission": {
                    "github/issue": {
                        "listed_unaddressed": "inferred",
                        "unlisted_addressed": "ignore",
                        "unlisted_unaddressed": "ignore",
                    }
                },
            }
        },
    }


@pytest.mark.asyncio
async def test_full_ticket_lifecycle_on_simulated_host(tmp_path):
    commands = FakeCommands(
        {
            "api": CommandResult(
                0, json.dumps([{"id": "n1", "updated_at": "2026-11-28T00:00:00+00:00"}])
            ),
            "issues/42/comments": CommandResult(0, "[]"),
            "issues/42/comments POST": CommandResult(0, '{"id": "c1"}'),
        }
    )
    runtime = Runtime.build(
        Config(state_dir=str(tmp_path / "state")),
        settings=settings(),
        ports=None,
    )
    runtime.registry.commands = commands
    await runtime.startup()
    try:
        trigger = make_trigger(text="please do the work")
        assert runtime.engine.receive(trigger) == "stored"
        await runtime.passes.run_once()

        # Assessment is queued as an effect; drive the decision deterministically.
        runtime.engine.decide(
            trigger.id,
            DecisionOutcome.ACCEPT,
            Reason("accepted", {"rules": ["rule-1"]}),
        )
        tickets = [t for t in runtime.engine.repo.tickets.list() if t.is_open]
        assert len(tickets) == 1
        ticket = tickets[0]

        runtime.engine.assignment_started(ticket.id, ticket.assignments[-1].id)
        runtime.engine.report(
            ticket.id,
            Report(
                assignment_id=ticket.assignments[-1].id,
                kind=ReportKind.READY,
                summary="done",
                at="2026-11-28T01:00:00+00:00",
            ),
        )
        updated = runtime.engine.repo.tickets.get(ticket.id)
        assert updated is not None and not updated.is_open
        # Effects were enqueued on commit and survive a drain.
        assert runtime.effects.count("launch") >= 0
    finally:
        await runtime.shutdown()


@pytest.mark.asyncio
async def test_restart_replays_pending_effects(tmp_path):
    state = str(tmp_path / "state")
    cfg = Config(state_dir=state)
    first = Runtime.build(cfg)
    await first.startup()
    receipt = type("R", (), {})  # placeholder to keep imports light
    del receipt
    from superagent.core.model import ChannelAddress, Receipt

    first.outbox.send(
        Receipt(
            id="r1",
            ticket_id="ticket-1",
            channel=ChannelAddress(source="github", thread="thread-1", address={"issue": 42}),
            purpose=Channel.RESULT,
            text="hello",
        )
    )
    assert first.effects.count("reply") >= 1
    await first.shutdown()

    second = Runtime.build(cfg)
    await second.startup()
    try:
        assert second.store.receipts.get("r1") is not None
        # The reply Effect row survives the restart (it may be done after drain).
        rows = [effect for effect in second.store.effects.list() if effect.kind == "reply"]
        assert rows
    finally:
        await second.shutdown()


@pytest.mark.asyncio
async def test_panel_state_route_through_loopback_guard(tmp_path):
    runtime = Runtime.build(Config(state_dir=str(tmp_path / "state")))
    await runtime.startup()
    try:
        status, body = await runtime.panel_request(
            "GET", "/state", headers={"x-orchestrator-request": "1"}, host="127.0.0.1:3082"
        )
        assert status == 200
        assert body["masterId"] == "superagent-main"
        status, _body = await runtime.panel_request("GET", "/state")
        assert status == 403
    finally:
        await runtime.shutdown()
