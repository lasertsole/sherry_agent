"""Source registry (prototype ``src/sources/registry.ts``).

Owns registered kinds, configured instances, health claims and the Outbox's
channel send surface.  It never imports the Runtime: all Engine access is
through a ``receive`` callable supplied at construction time.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from pydantic import ValidationError

from superagent.core.model import ChannelAddress, Receipt, Trigger
from superagent.infra.commands import Commands
from superagent.infra.health import HealthBoard
from superagent.sources.host import SourceContext
from superagent.sources.kind import Issue, SendResult, SourceInstance, SourceKind, ThreadContextFn


class SourceRegistry:
    """Registry of Source kinds and instances."""

    def __init__(
        self,
        commands: Commands,
        board: HealthBoard,
        *,
        receive: Callable[[Trigger], str] | None = None,
        config_get: Callable[[str], dict[str, Any] | None] | None = None,
    ) -> None:
        self.commands = commands
        self.board = board
        self._receive = receive or (lambda _trigger: "filtered")
        self._config_get = config_get
        self._kinds: dict[str, SourceKind] = {}
        self._instances: dict[str, SourceInstance] = {}
        self._contexts: dict[str, SourceContext] = {}

    def register(self, kind: SourceKind) -> None:
        """Register a kind and claim its health names."""
        if kind.kind in self._kinds:
            raise ValueError(f"source kind already registered: {kind.kind}")
        for name in kind.health:
            owner = kind.kind if name == kind.kind else f"{kind.kind}:{name}"
            self.board.claim(name, owner)
        self._kinds[kind.kind] = kind

    def unregister(self, kind_name: str) -> None:
        """Remove a kind; configured instances are kept for inspection."""
        self._kinds.pop(kind_name, None)
        self._contexts.pop(kind_name, None)

    def kind(self, name: str) -> SourceKind | None:
        """Return a registered kind by name."""
        return self._kinds.get(name)

    def configure(self, instance: SourceInstance) -> None:
        """Add or replace a configured instance."""
        if instance.kind not in self._kinds:
            raise ValueError(f"unknown source kind: {instance.kind}")
        self._instances[instance.id] = instance

    def instances(self) -> list[SourceInstance]:
        """Return all configured instances."""
        return list(self._instances.values())

    def instance(self, id_: str) -> SourceInstance | None:
        """Return one configured instance."""
        return self._instances.get(id_)

    def context(self, kind_name: str) -> SourceContext:
        """Return (and memoise) the context handed to a kind."""
        if kind_name not in self._contexts:
            self._contexts[kind_name] = SourceContext(
                commands=self.commands,
                board=self.board,
                receive=self._receive,
                config=self._config_get or (lambda _kind: None),
            )
        return self._contexts[kind_name]

    def validate(self, kind_name: str, config: dict[str, Any]) -> list[Issue]:
        """Validate an instance config against the kind's pydantic schema."""
        kind = self.kind(kind_name)
        if kind is None:
            return [Issue(path="kind", message=f"unknown source kind: {kind_name}")]
        try:
            kind.schema.model_validate(config)
        except ValidationError as exc:
            return [
                Issue(path=".".join(str(part) for part in error["loc"]), message=error["msg"])
                for error in exc.errors()
            ]
        return []

    @staticmethod
    def _parsed(kind: SourceKind, config: dict[str, Any]) -> Any:
        if isinstance(config, kind.schema):
            return config
        return kind.schema.model_validate(config)

    def channel_allowed(self, addr: ChannelAddress, config: dict[str, Any]) -> bool:
        """Return whether the destination channel is still listened to."""
        kind = self.kind(addr.source)
        if kind is None or kind.channel is None:
            return False
        return kind.channel.allowed(addr, self._parsed(kind, config))

    async def send(self, message: Receipt, config: dict[str, Any]) -> str:
        """Send one Receipt through its Source; return sent/gone/waiting."""
        kind = self.kind(message.channel.source)
        if kind is None or kind.channel is None:
            return "gone"
        if not self.channel_allowed(message.channel, config):
            return "gone"
        try:
            result: SendResult = await kind.channel.send(
                self.commands, message, self._parsed(kind, config)
            )
        except Exception:  # noqa: BLE001 — Outbox retries any transport failure
            return "waiting"
        return result.status

    def link(self, addr: ChannelAddress) -> dict[str, str]:
        """Return the panel link for a channel address."""
        kind = self.kind(addr.source)
        if kind is None or kind.channel is None:
            return {}
        return kind.channel.link(addr)

    def thread_context(self, kind_name: str) -> ThreadContextFn | None:
        """Return the thread-context reader for a kind, if any."""
        kind = self.kind(kind_name)
        return kind.thread_context if kind is not None else None

    async def find(self, query: str) -> dict[str, Any]:
        """Search all kinds that support references."""
        threads: list[dict[str, Any]] = []
        terms: list[str] = []
        for name, kind in self._kinds.items():
            if kind.find is None:
                continue
            cfg = self._config_get(name) if self._config_get else None
            if not cfg:
                continue
            result = await kind.find(query, cfg)
            threads.extend(result.get("threads", []))
            terms.extend(result.get("terms", []))
        return {"threads": threads, "terms": terms}

    def scope(self, kind_name: str) -> Callable[[dict[str, Any], dict[str, Any]], dict[str, Any]]:
        """Return the scope function for a kind."""
        kind = self.kind(kind_name)
        if kind is None:
            return lambda _meta, _config: {"listened": False, "reason": "unknownSource"}
        return kind.scope

    def environment_values(
        self, kind_name: str, config: dict[str, Any], trigger: Trigger | None
    ) -> dict[str, str]:
        """Return Source-provided execution variables."""
        kind = self.kind(kind_name)
        if kind is None or kind.environment is None:
            return {}
        return kind.environment.values(config, trigger)


__all__ = ["SourceRegistry"]
