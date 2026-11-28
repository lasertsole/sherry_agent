"""Feishu topic reply channel (prototype ``src/sources/feishu-channel.ts``)."""

from __future__ import annotations

from superagent.core.model import ChannelAddress, Receipt
from superagent.infra.commands import Commands
from superagent.sources.feishu_trigger import FeishuConfig
from superagent.sources.kind import SendResult

GONE_CODES = (404, 410)


def marker(receipt_id: str) -> str:
    """Return the hidden idempotency marker for a Receipt."""
    return f"<!-- dsh-superagent: {receipt_id} -->"


class FeishuChannel:
    """Post topic replies through the bot CLI."""

    def __init__(self) -> None:
        self._sent: dict[str, str] = {}

    def allowed(self, addr: ChannelAddress, config: FeishuConfig) -> bool:
        """Return whether the destination chat is still configured."""
        chat_id = str(addr.address.get("chat_id", ""))
        return (
            any(chat.chat_id == chat_id for chat in config.chats)
            or config.other_chats == "mentions"
        )

    async def send(self, commands: Commands, message: Receipt, config: FeishuConfig) -> SendResult:
        """Send one reply; 404/410 maps to ``gone``, other failures raise."""
        if message.id in self._sent:
            return SendResult(status="sent", message_id=self._sent[message.id])
        root = str(message.channel.address.get("root_id", ""))
        text = f"{marker(message.id)}\n{message.text}"
        result = await commands.run([config.cli, "message", "reply", root, "--text", text])
        if result.code in GONE_CODES:
            return SendResult(status="gone")
        if not result.ok:
            raise RuntimeError(f"feishu send failed with code {result.code}")
        parsed = result.json()
        message_id = str(parsed.get("message_id", "")) if isinstance(parsed, dict) else ""
        self._sent[message.id] = message_id
        return SendResult(status="sent", message_id=message_id)

    def link(self, addr: ChannelAddress) -> dict[str, str]:
        """Return a panel link label for the topic."""
        return {
            "url": str(addr.address.get("url", "")),
            "label": str(addr.address.get("chat_name") or addr.thread),
        }

    def replay(self, texts: list[str]) -> None:
        """Learn markers from historical messages after a restart."""
        for text in texts:
            needle = "<!-- dsh-superagent: "
            if needle in text:
                self._sent.setdefault(text.split(needle, 1)[1].split(" -->", 1)[0], "")


def chat_limit(text: str, limit: int = 3000) -> str:
    """Trim a reply to the Feishu chat limit."""
    return text[:limit]


__all__ = ["GONE_CODES", "FeishuChannel", "chat_limit", "marker"]
