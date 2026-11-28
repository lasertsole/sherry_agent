"""Feishu event → Trigger translation (prototype ``src/sources/feishu-trigger.ts``)."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from superagent.core.ids import key
from superagent.core.model import Actor, AddressKind, ChannelAddress, SourceRef, Trigger


class FeishuChat(BaseModel):
    """One configured chat."""

    model_config = ConfigDict(extra="forbid")

    chat_id: str
    name: str = ""
    listen: str = Field(default="mentions", pattern="^(mentions|all)$")


class FeishuConfig(BaseModel):
    """Configuration for one Feishu Source instance."""

    model_config = ConfigDict(extra="forbid")

    cli: str = "lark-cli"
    bot_open_id: str = ""
    chats: list[FeishuChat] = Field(default_factory=list)
    direct_messages: bool = False
    other_chats: str = Field(default="none", pattern="^(none|mentions)$")


def thread_ref(chat_id: str, root_id: str) -> SourceRef:
    """Return the canonical Thread reference for a Feishu topic."""
    return SourceRef(source="feishu", id=f"{chat_id}:{root_id}")


def feishu_trigger(
    event: dict[str, Any], config: FeishuConfig, *, history: bool = False
) -> Trigger | None:
    """Translate one Feishu message event into a Trigger."""
    message_id = str(event.get("message_id", ""))
    chat_id = str(event.get("chat_id", ""))
    if not message_id or not chat_id:
        return None
    root_id = str(event.get("root_id") or message_id)
    configured = {item.chat_id: item for item in config.chats}
    chat = configured.get(chat_id)
    if chat is None and not config.direct_messages and config.other_chats == "none":
        return None
    sender = event.get("sender") or {}
    if not isinstance(sender, dict):
        sender = {}
    open_id = str((sender.get("sender_id") or {}).get("open_id", ""))
    bot = str(sender.get("sender_type", "")) == "bot" or bool(sender.get("bot"))
    if open_id and open_id == config.bot_open_id:
        return None
    mentions = [
        str((item.get("id") or {}).get("open_id", ""))
        for item in event.get("mentions", [])
        if isinstance(item, dict)
    ]
    addressed = (
        AddressKind.MENTION
        if config.bot_open_id and config.bot_open_id in mentions
        else AddressKind.DIRECT
        if config.direct_messages and chat is None
        else None
    )
    listen = chat.listen if chat is not None else "mentions"
    if addressed is None and listen != "all" and not history:
        return None
    body = event.get("body") or {}
    text = str(body.get("content", "")) if isinstance(body, dict) else ""
    thread = thread_ref(chat_id, root_id)
    return Trigger(
        id=key("feishu", message_id),
        source="feishu",
        threads=[thread],
        actor=Actor(source="feishu", id="" if bot else open_id),
        human=not bot and bool(open_id),
        addressed=addressed,
        at=str(event.get("create_time", "")),
        text=text,
        channel=ChannelAddress(
            source="feishu",
            thread=thread.id,
            address={
                "chat_id": chat_id,
                "message_id": message_id,
                "root_id": root_id,
                "chat_name": chat.name if chat else "",
            },
        ),
        url=str(event.get("url", "")) or None,
        meta={
            "chat_id": chat_id,
            "message_id": message_id,
            "root_id": root_id,
            "bot": bot,
            "history": history,
        },
    )


__all__ = ["FeishuChat", "FeishuConfig", "feishu_trigger", "thread_ref"]
