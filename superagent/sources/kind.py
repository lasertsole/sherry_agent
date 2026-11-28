"""SourceKind protocol and field specs (prototype ``src/sources/kind.ts``).

A Source is a reusable kind plus one configured instance.  The kind describes
its configuration schema, the panel form, health claims and the optional
behaviours the Engine/Outbox may call.  All behaviours are optional so a
minimal Source (webhook) can implement only translation and channel send.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

from pydantic import BaseModel

from superagent.core.model import ChannelAddress, Receipt, Trigger
from superagent.infra.commands import Commands
from superagent.infra.health import HealthBoard


@dataclass(frozen=True)
class FieldSpec:
    """One panel form field."""

    name: str
    kind: str = "text"  # rows/text/select/number/advanced
    label: str = ""
    options: tuple[str, ...] = ()
    advanced: bool = False


@dataclass(frozen=True)
class Issue:
    """A configuration validation issue."""

    path: str
    message: str

    def __str__(self) -> str:
        """Render the issue as ``path: message``."""
        return f"{self.path}: {self.message}"


@dataclass(frozen=True)
class SendResult:
    """Outcome of a Channel send."""

    status: str  # sent | gone
    message_id: str | None = None


@dataclass(frozen=True)
class ThreadContext:
    """One page of messages read from a Source thread."""

    messages: list[dict[str, Any]] = field(default_factory=list)
    next_cursor: str | None = None
    body: str = ""
    status: str | None = None


class ScopeFn(Protocol):
    """Return the admission scope of a Trigger."""

    def __call__(self, meta: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]: ...


class ChannelFn(Protocol):
    """Channel behaviour bundle."""

    def allowed(self, addr: ChannelAddress, config: dict[str, Any]) -> bool: ...

    async def send(
        self, commands: Commands, message: Receipt, config: dict[str, Any]
    ) -> SendResult: ...

    def link(self, addr: ChannelAddress) -> dict[str, str]: ...


class ThreadContextFn(Protocol):
    """Read a bounded window of a Source thread."""

    async def read(
        self,
        commands: Commands,
        trigger: Trigger,
        part: str,
        cursor: str | None,
    ) -> ThreadContext: ...

    def message_id(self, trigger: Trigger) -> str: ...


class FindFn(Protocol):
    """Search Source-specific references."""

    async def __call__(self, query: str, config: dict[str, Any]) -> dict[str, Any]: ...


class EnvironmentFn(Protocol):
    """Declare and resolve Ticket-specific environment variables."""

    def variables(self) -> list[str]: ...

    def values(self, config: dict[str, Any], trigger: Trigger | None) -> dict[str, str]: ...


class SourceKind(Protocol):
    """A Source implementation."""

    kind: str
    title: dict[str, str]
    description: dict[str, str]
    schema: type[BaseModel]
    fields: list[FieldSpec]
    health: list[str]

    def validate(self, config: dict[str, Any]) -> list[Issue]: ...

    def scope(self, meta: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]: ...

    @property
    def channel(self) -> ChannelFn | None: ...

    @property
    def thread_context(self) -> ThreadContextFn | None: ...

    @property
    def find(self) -> FindFn | None: ...

    @property
    def environment(self) -> EnvironmentFn | None: ...

    def claims(self, board: HealthBoard) -> None: ...


@dataclass(frozen=True)
class SourceInstance:
    """One configured Source instance."""

    id: str
    kind: str
    enabled: bool
    config: dict[str, Any]


__all__ = [
    "ChannelFn",
    "EnvironmentFn",
    "FieldSpec",
    "FindFn",
    "Issue",
    "ScopeFn",
    "SendResult",
    "SourceInstance",
    "SourceKind",
    "ThreadContext",
    "ThreadContextFn",
]
