"""Feishu Source kind and plugin entry (prototype ``src/sources/feishu.ts``)."""

from __future__ import annotations

import asyncio
from typing import Any

from pydantic import BaseModel

from superagent.infra.health import HealthBoard
from superagent.sources.feishu_channel import FeishuChannel
from superagent.sources.feishu_loops import FeishuLoops
from superagent.sources.feishu_read import FeishuThreadContext
from superagent.sources.feishu_trigger import FeishuConfig
from superagent.sources.host import HealthState, SourceContext
from superagent.sources.kind import FieldSpec, Issue


class FeishuSource:
    """Feishu SourceKind: config schema, channel, thread context and watch."""

    kind = "feishu"
    title = {"zh": "飞书", "en": "Feishu"}
    description = {
        "zh": "从飞书会话与话题拉取工作。",
        "en": "Pull work from Feishu chats and topics.",
    }
    schema: type[BaseModel] = FeishuConfig
    fields = [
        FieldSpec("cli", "text", "Bot CLI"),
        FieldSpec("bot_open_id", "text", "Bot open_id"),
        FieldSpec("chats", "advanced", "Chats"),
        FieldSpec("direct_messages", "select", "Direct messages"),
        FieldSpec("other_chats", "select", "Other chats", options=("none", "mentions")),
    ]
    health = ["feishu"]

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        self.config = config or {}
        self.channel = FeishuChannel()
        self.failures = 0

    def claims(self, board: HealthBoard) -> None:
        """Claim the Feishu health entry."""
        board.claim("feishu", "feishu")

    def validate(self, config: dict[str, Any]) -> list[Issue]:
        """Validate a config dict."""
        try:
            FeishuConfig.model_validate(config)
        except Exception as exc:  # noqa: BLE001 — surface as panel issues
            return [Issue(path="config", message=str(exc)[:300])]
        return []

    def scope(self, meta: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]:
        """Return the admission scope for one Feishu event."""
        addressed = meta.get("addressed")
        if addressed == "direct":
            return {"listened": True, "observed": True, "row": "direct"}
        if addressed == "mention":
            return {"listened": True, "observed": True, "row": str(meta.get("chat_id", "other"))}
        return {"listened": False, "reason": "notAddressed"}

    @property
    def thread_context(self) -> FeishuThreadContext | None:
        """Return a thread-context reader when configured."""
        if not self.config:
            return None
        return FeishuThreadContext(FeishuConfig.model_validate(self.config))

    async def find(self, query: str, config: dict[str, Any]) -> dict[str, Any]:
        """Match message ids and topic links."""
        threads: list[dict[str, Any]] = []
        if query.startswith("om_"):
            threads.append({"source": "feishu", "id": query})
        return {"threads": threads, "terms": [query]}

    async def watch(
        self,
        ctx: SourceContext,
        signal: asyncio.Event | None = None,
        *,
        config: dict[str, Any] | None = None,
    ) -> list[Any]:
        """Consume one page of live Feishu events."""
        cfg = FeishuConfig.model_validate(config or self.config)
        loops = FeishuLoops(cfg)
        try:
            triggers = await loops.watch(ctx, signal)
        except Exception as exc:  # noqa: BLE001 — report and retry next pass
            self.failures += 1
            ctx.report("feishu", HealthState("feishu", "error", str(exc)[:200]))
            return []
        ctx.report("feishu", HealthState("feishu", "ready", ""))
        return triggers

    @property
    def environment(self) -> FeishuSourceEnvironment:
        """Return the Source's execution-variable provider."""
        return FeishuSourceEnvironment()


class FeishuSourceEnvironment:
    """Declare Feishu-specific worker variables."""

    def variables(self) -> list[str]:
        """Return variable names."""
        return ["FEISHU_BOT", "FEISHU_CHAT"]

    def values(self, config: dict[str, Any], trigger: Any = None) -> dict[str, str]:
        """Resolve values for one Ticket."""
        values = {"FEISHU_BOT": str(config.get("bot_open_id", ""))}
        if trigger is not None and getattr(trigger, "channel", None) is not None:
            values["FEISHU_CHAT"] = str(trigger.channel.address.get("chat_id", ""))
        return values


def apply(config: dict[str, Any]) -> FeishuSource:
    """Plugin entry point."""
    FeishuConfig.model_validate(config)
    return FeishuSource(config)


__all__ = ["FeishuSource", "FeishuSourceEnvironment", "apply"]
