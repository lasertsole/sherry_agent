"""Feishu trigger, channel, thread and image tests."""

from __future__ import annotations

import json

import pytest

from superagent.core.model import ChannelAddress, Receipt, Channel
from superagent.infra.commands import CommandResult, FakeCommands
from superagent.infra.health import HealthBoard
from superagent.sources.feishu import FeishuSource
from superagent.sources.feishu_channel import FeishuChannel, marker
from superagent.sources.feishu_image import image_keys, read_image
from superagent.sources.feishu_read import FeishuThreadContext
from superagent.sources.feishu_trigger import FeishuConfig, feishu_trigger
from superagent.sources.registry import SourceRegistry

CONFIG = FeishuConfig(
    cli="lark-cli",
    bot_open_id="ou_bot",
    chats=[{"chat_id": "chat-1", "name": "Eng", "listen": "mentions"}],
    direct_messages=True,
)


def event(**overrides):
    value = {
        "message_id": "om_1",
        "chat_id": "chat-1",
        "root_id": "om_root",
        "create_time": "2026-11-28T00:00:00+00:00",
        "body": {"content": "please help"},
        "sender": {"sender_id": {"open_id": "ou_alice"}, "sender_type": "user"},
        "mentions": [{"id": {"open_id": "ou_bot"}}],
    }
    value.update(overrides)
    return value


def test_trigger_mention_direct_bot_and_self() -> None:
    trigger = feishu_trigger(event(), CONFIG)
    assert trigger is not None
    assert trigger.addressed is not None and trigger.addressed.value == "mention"
    assert trigger.threads[0].id == "chat-1:om_root"
    assert trigger.human is True
    assert (
        feishu_trigger(
            event(sender={"sender_type": "bot", "sender_id": {"open_id": "x"}}), CONFIG
        ).human
        is False
    )
    assert feishu_trigger(event(sender={"sender_id": {"open_id": "ou_bot"}}), CONFIG) is None
    dm = feishu_trigger(event(chat_id="dm-1", mentions=[]), CONFIG)
    assert dm is not None and dm.addressed is not None and dm.addressed.value == "direct"
    ignored = feishu_trigger(event(chat_id="chat-1", mentions=[]), CONFIG)
    assert ignored is None
    assert image_keys("see img_v2_abc and img_123") == ["img_v2_abc", "img_123"]


@pytest.mark.asyncio
async def test_channel_sent_gone_and_replay() -> None:
    channel = FeishuChannel()
    receipt = Receipt(
        id="msg-f1",
        ticket_id="t1",
        channel=ChannelAddress(
            source="feishu",
            thread="chat-1:om_root",
            address={"chat_id": "chat-1", "root_id": "om_root"},
        ),
        purpose=Channel.RESULT,
        text="done",
    )
    commands = FakeCommands({"message reply": CommandResult(0, '{"message_id": "om_reply"}')})
    result = await channel.send(commands, receipt, CONFIG)
    assert result.status == "sent" and result.message_id == "om_reply"
    assert marker("msg-f1") in commands.calls[0][0][-1]

    commands.responses["message reply"] = CommandResult(410, "")
    gone = Receipt(**{**receipt.__dict__, "id": "msg-f2"})
    assert (await channel.send(commands, gone, CONFIG)).status == "gone"

    channel.replay([f"{marker('msg-f3')} old"])
    again = Receipt(**{**receipt.__dict__, "id": "msg-f3"})
    assert (await channel.send(FakeCommands(), again, CONFIG)).status == "sent"


@pytest.mark.asyncio
async def test_thread_context_and_image_cli() -> None:
    commands = FakeCommands(
        {
            "message thread": CommandResult(
                0,
                json.dumps({"items": [{"message_id": "om_1", "body": {"content": "hi"}}]}),
            ),
            "image get": CommandResult(0, "binary"),
        }
    )
    trigger = feishu_trigger(event(), CONFIG)
    assert trigger is not None
    context = await FeishuThreadContext(CONFIG).read(commands, trigger, "first", None)
    assert context.messages[0]["id"] == "om_1"
    assert await read_image(commands, CONFIG, "img_123") == b"binary"
    with pytest.raises(ValueError):
        await read_image(commands, CONFIG, "../bad")


@pytest.mark.asyncio
async def test_feishu_source_watch_reports_health() -> None:
    source = FeishuSource(CONFIG.model_dump())
    states: list[str] = []
    ctx = __import__("superagent.sources.host", fromlist=["SourceContext"]).SourceContext(
        commands=FakeCommands({"event list": CommandResult(0, json.dumps([event()]))}),
        board=HealthBoard(),
        report=lambda _name, state: states.append(state.state),
    )
    triggers = await source.watch(ctx, config=CONFIG.model_dump())
    assert len(triggers) == 1
    assert states == ["ready"]

    ctx.commands = FakeCommands({"event list": CommandResult(1, "boom")})
    assert await source.watch(ctx, config=CONFIG.model_dump()) == []
    assert states[-1] == "error"


@pytest.mark.asyncio
async def test_registry_accepts_feishu_kind() -> None:
    registry = SourceRegistry(FakeCommands(), HealthBoard())
    registry.register(FeishuSource())
    assert registry.kind("feishu") is not None
    registry.configure(
        __import__("superagent.sources.kind", fromlist=["SourceInstance"]).SourceInstance(
            "f1", "feishu", True, CONFIG.model_dump()
        )
    )
    assert registry.instances()[0].id == "f1"
