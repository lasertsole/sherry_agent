"""Feishu thread reads (prototype ``src/sources/feishu-read.ts``)."""

from __future__ import annotations

from superagent.core.model import Trigger
from superagent.infra.commands import Commands
from superagent.sources.feishu_trigger import FeishuConfig
from superagent.sources.kind import ThreadContext


class FeishuThreadContext:
    """Read root/parent/topic messages for assessment."""

    def __init__(self, config: FeishuConfig) -> None:
        self.config = config

    def message_id(self, trigger: Trigger) -> str:
        """Return the Source message id of a Trigger."""
        return str(trigger.meta.get("message_id", trigger.id))

    async def read(
        self,
        commands: Commands,
        trigger: Trigger,
        part: str,
        cursor: str | None,
    ) -> ThreadContext:
        """Read one page of the topic containing the Trigger."""
        root = str(trigger.meta.get("root_id", ""))
        argv = [self.config.cli, "message", "thread", root]
        if cursor:
            argv += ["--cursor", cursor]
        result = await commands.run(argv)
        if not result.ok:
            raise RuntimeError(f"feishu thread read failed: {result.stderr[:200]}")
        parsed = result.json()
        rows = parsed.get("items", []) if isinstance(parsed, dict) else []
        messages = [
            {
                "id": str(item.get("message_id", "")),
                "frm": str((item.get("sender") or {}).get("sender_id", {}).get("open_id", "")),
                "at": str(item.get("create_time", "")),
                "text": str((item.get("body") or {}).get("content", "")),
                "reply_to": str(item.get("parent_id", "")) or None,
            }
            for item in rows
            if isinstance(item, dict)
        ]
        next_cursor = str(parsed.get("page_token", "")) if isinstance(parsed, dict) else ""
        return ThreadContext(messages=messages, next_cursor=next_cursor or None)


__all__ = ["FeishuThreadContext"]
