"""Feishu event consumption and history recovery (prototype ``src/sources/feishu-loops.ts``)."""

from __future__ import annotations

import asyncio
from typing import Any

from superagent.core.model import Trigger
from superagent.infra.commands import Commands
from superagent.sources.feishu_trigger import FeishuConfig, feishu_trigger
from superagent.sources.host import SourceContext


class FeishuLoops:
    """Consume live events, recover history and poll subscribed topics."""

    def __init__(self, config: FeishuConfig) -> None:
        self.config = config
        self.failures = 0

    async def events(self, commands: Commands, since: str | None = None) -> list[dict[str, Any]]:
        """Read one page of bot events."""
        argv = [self.config.cli, "event", "list"]
        if since:
            argv += ["--since", since]
        result = await commands.run(argv)
        if not result.ok:
            self.failures += 1
            raise RuntimeError(f"feishu event read failed: {result.stderr[:200]}")
        self.failures = 0
        parsed = result.json()
        return (
            [item for item in parsed if isinstance(item, dict)] if isinstance(parsed, list) else []
        )

    async def history(
        self, commands: Commands, chat_id: str, limit: int = 50
    ) -> list[dict[str, Any]]:
        """Recover recent history for one chat."""
        result = await commands.run(
            [self.config.cli, "message", "list", chat_id, "--limit", str(limit)]
        )
        if not result.ok:
            raise RuntimeError(f"feishu history read failed: {result.stderr[:200]}")
        parsed = result.json()
        return (
            [item for item in parsed if isinstance(item, dict)] if isinstance(parsed, list) else []
        )

    async def watch(
        self,
        ctx: SourceContext,
        signal: asyncio.Event | None = None,
    ) -> list[Trigger]:
        """Translate one page of live events into Triggers."""
        triggers: list[Trigger] = []
        for event in await self.events(ctx.commands):
            if signal is not None and signal.is_set():
                break
            trigger = feishu_trigger(event, self.config)
            if trigger is not None:
                ctx.emit(trigger)
                triggers.append(trigger)
        return triggers


__all__ = ["FeishuLoops"]
