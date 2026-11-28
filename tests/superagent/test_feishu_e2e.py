"""P3 chain: Feishu mention → Ticket (mock CLI, mock Sherry subagent)."""

from __future__ import annotations

import json

import pytest

from superagent.core.memory import MemoryRepositories
from superagent.core.model import AddressKind, DecisionOutcome, Reason
from superagent.infra.commands import CommandResult, FakeCommands
from superagent.infra.health import HealthBoard
from superagent.sources.feishu import FeishuSource
from superagent.sources.feishu_trigger import FeishuConfig
from superagent.sources.host import SourceContext
from tests.superagent.conftest import make_engine, policy_settings


@pytest.mark.asyncio
async def test_feishu_mention_creates_ticket() -> None:
    config = FeishuConfig(
        cli="lark-cli",
        bot_open_id="ou_bot",
        chats=[{"chat_id": "chat-1", "name": "Eng", "listen": "mentions"}],
    )
    event = {
        "message_id": "om_1",
        "chat_id": "chat-1",
        "root_id": "om_root",
        "create_time": "2026-11-28T00:00:00+00:00",
        "body": {"content": "@bot please handle this"},
        "sender": {"sender_id": {"open_id": "ou_alice"}, "sender_type": "user"},
        "mentions": [{"id": {"open_id": "ou_bot"}}],
    }
    settings = policy_settings(accept_mode="auto")
    settings["listening"] = ["feishu"]
    settings["sources"]["feishu"] = {
        **settings["sources"]["github"],
        "allowed": [{"id": "ou_alice"}],
    }
    repo = MemoryRepositories()
    engine, ports = make_engine(settings=settings, repo=repo)
    seen: list[object] = []
    ctx = SourceContext(
        commands=FakeCommands({"event list": CommandResult(0, json.dumps([event]))}),
        board=HealthBoard(),
        receive=lambda trigger: seen.append(trigger) or engine.receive(trigger),
    )
    source = FeishuSource(config.model_dump())
    triggers = await source.watch(ctx, config=config.model_dump())
    assert len(triggers) == 1
    trigger = triggers[0]
    assert trigger.addressed == AddressKind.MENTION

    engine.evaluate_due()
    engine.decide(trigger.id, DecisionOutcome.ACCEPT, Reason("accepted", {"rules": ["rule-1"]}))
    tickets = [ticket for ticket in repo.tickets.list() if ticket.is_open]
    assert len(tickets) == 1
    assert tickets[0].channels[0].source == "feishu"
    assert any(item[0] == "launch" for item in ports["effects"].items)
