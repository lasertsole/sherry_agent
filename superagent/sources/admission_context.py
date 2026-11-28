"""Bounded admission thread window (prototype ``src/sources/admission_context.ts``)."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

from superagent.core.model import Trigger
from superagent.limits import (
    AROUND_CHARS,
    MESSAGE_CHARS,
    STUB_CHARS,
    THREAD_AROUND_MESSAGES,
    THREAD_FIRST_MESSAGES,
)
from superagent.sources.kind import ThreadContext
from superagent.sources.registry import SourceRegistry


@dataclass
class ContextMessage:
    """One message in an assessment window."""

    id: str
    frm: str
    at: str
    text: str
    bot: bool = False
    reply_to: str | None = None
    url: str | None = None
    images: list[str] = field(default_factory=list)

    def stub(self) -> str:
        """Return the short form used outside the focus window."""
        return self.text[:STUB_CHARS]


@dataclass
class ThreadWindow:
    """First page plus the messages around the Trigger."""

    first_five: list[ContextMessage] = field(default_factory=list)
    around: list[ContextMessage] = field(default_factory=list)
    other: list[ContextMessage] = field(default_factory=list)
    status: str | None = None

    @classmethod
    def empty(cls) -> ThreadWindow:
        """Return an empty window."""
        return cls()

    def all_messages(self) -> list[ContextMessage]:
        """Return first page plus focus messages."""
        return [*self.first_five, *self.around]


@dataclass(frozen=True)
class AdmissionContextLimits:
    """Machine limits applied when building a window."""

    max_pages: int = 2
    timeout_ms: int = 4000


class AdmissionContext:
    """Read the first pages of a Thread and the Trigger's neighbours."""

    def __init__(
        self,
        sources: SourceRegistry,
        *,
        max_pages: int = 2,
        timeout_ms: int = 4000,
    ) -> None:
        self.sources = sources
        self.limits = AdmissionContextLimits(max_pages=max_pages, timeout_ms=timeout_ms)

    async def build(self, trigger: Trigger) -> ThreadWindow:
        """Build a bounded window, falling back to empty on timeout."""
        reader = self.sources.thread_context(trigger.source)
        if reader is None:
            return ThreadWindow.empty()
        try:
            page: ThreadContext = await asyncio.wait_for(
                reader.read(self.sources.commands, trigger, "first", None),
                timeout=self.limits.timeout_ms / 1000,
            )
        except Exception:  # noqa: BLE001 — any Source failure degrades to an empty window
            return ThreadWindow(status="pollError")

        messages = [
            ContextMessage(
                id=str(item.get("id", "")),
                frm=str(item.get("frm", "")),
                at=str(item.get("at", "")),
                text=str(item.get("text", ""))[:MESSAGE_CHARS],
                bot=bool(item.get("bot", False)),
                reply_to=item.get("reply_to"),
                url=item.get("url"),
                images=list(item.get("images", [])),
            )
            for item in page.messages
        ]
        first_five = messages[:THREAD_FIRST_MESSAGES]
        own_id = reader.message_id(trigger)
        index = next((i for i, item in enumerate(messages) if item.id == own_id), -1)
        if index < 0:
            around = messages[:THREAD_AROUND_MESSAGES]
            other = messages[THREAD_AROUND_MESSAGES:]
        else:
            start = max(0, index - THREAD_AROUND_MESSAGES)
            end = min(len(messages), index + THREAD_AROUND_MESSAGES + 1)
            around = [
                ContextMessage(
                    id=item.id,
                    frm=item.frm,
                    at=item.at,
                    text=item.text[:AROUND_CHARS],
                    bot=item.bot,
                    reply_to=item.reply_to,
                    url=item.url,
                    images=list(item.images),
                )
                for item in messages[start:end]
            ]
            other = [item for item in messages if item not in around and item not in first_five]
        return ThreadWindow(first_five=first_five, around=around, other=other, status=page.status)


__all__ = ["AdmissionContext", "AdmissionContextLimits", "ContextMessage", "ThreadWindow"]
