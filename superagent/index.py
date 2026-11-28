"""Assemble and run the whole superagent process (prototype ``src/index.ts``).

This module is the composition root: it owns the SQLite ``Store``, wires the
Engine's ports to their real implementations, starts the 2-second passes, and
exposes a single ``startup`` / ``shutdown`` pair.  It deliberately keeps every
adapter behind a narrow port so the process can boot in ``degraded`` mode when
Sherry is unavailable — Triggers still land in the Store, nothing is lost.

``main()`` is a thin, blocking wrapper for ``python -m superagent``; the async
lifecycle lives in :class:`Runtime` so tests can drive it on a simulated host.
"""

from __future__ import annotations

import asyncio
import signal
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from superagent.app.effects import Effects
from superagent.app.outbox import Outbox
from superagent.app.passes import Passes
from superagent.app.prompts import Prompts
from superagent.app.settings import SettingsStore
from superagent.app.store import Store
from superagent.core.engine import Engine
from superagent.core.part import EngineOptions
from superagent.deployment import Config, execution_root, resolve_deployment
from superagent.infra.commands import Commands, SubprocessCommands
from superagent.infra.health import HealthBoard
from superagent.infra.paths import StatePaths, state_paths
from superagent.limits import DEFAULT_PASS_INTERVAL_S
from superagent.panel.api import PanelAPI
from superagent.panel.http import PanelHTTP
from superagent.policy import settings_policy
from superagent.sources.registry import SourceRegistry

DEFAULT_SETTINGS: dict[str, Any] = {
    "accept_mode": "observe",
    "paused": False,
    "eval_timeout_ms": 600_000,
    "listening": [],
    "rules": {},
    "sources": {},
}


class _StoreRepos:
    """Expose the SQLite Store through the Engine's ``Repositories`` shape.

    The properties are intentionally ``Any``: the Engine only needs the
    repository duck-types, and a strict structural annotation would couple the
    composition root to three unrelated generic protocols.
    """

    def __init__(self, store: Store) -> None:
        self._store = store

    @property
    def atomic(self) -> Any:
        """Return the Store's nested transaction context."""
        return self._store.atomic

    @property
    def triggers(self) -> Any:
        """Trigger repository."""
        return self._store.triggers

    @property
    def threads(self) -> Any:
        """Thread repository."""
        return self._store.threads

    @property
    def trigger_threads(self) -> Any:
        """Trigger↔Thread join repository."""
        return self._store.trigger_threads

    @property
    def tickets(self) -> Any:
        """Ticket repository."""
        return self._store.tickets

    @property
    def decisions(self) -> Any:
        """Decision repository."""
        return self._store.decisions

    @property
    def effects(self) -> Any:
        """Effect repository."""
        return self._store.effects

    @property
    def outbox(self) -> Any:
        """Receipt repository (the outbox's single writer)."""
        return self._store.receipts


class _OutboxSources:
    """Adapt the SourceRegistry to the Outbox's no-config port."""

    def __init__(self, registry: SourceRegistry, config: dict[str, Any]) -> None:
        self.registry = registry
        self.config = config

    def channel_allowed(self, channel: Any) -> bool:
        """Return whether the destination is still listened to."""
        return self.registry.channel_allowed(channel, self.config)

    def send(self, receipt: Any) -> Any:
        """Send a Receipt through its Source."""
        return self.registry.send(receipt, self.config)


class _PassRuntime:
    """Runtime surface required by the pass scheduler."""

    def __init__(self, effects: Effects, engine: Engine) -> None:
        self.effects = effects
        self._engine = engine

    def evaluate_due(self) -> None:
        """Advance due Evaluations on every pass."""
        self._engine.evaluate_due()


@dataclass
class Ports:
    """Stand-in adapters a host may replace before ``startup``."""

    commands: Commands | None = None
    assessor: Any = None
    scope: Any = None
    executor: Any = None
    sherry_client: Any = None
    settings_getter: Callable[[], Any] | None = None
    prepare_ticket: Callable[[Any, Any], dict[str, Any]] | None = None
    emit: Callable[[Any], None] | None = None


@dataclass
class Runtime:
    """The running superagent process and all of its long-lived objects."""

    config: Config
    paths: StatePaths
    store: Store
    board: HealthBoard
    effects: Effects
    registry: SourceRegistry
    outbox: Outbox
    engine: Engine
    passes: Passes
    panel: PanelHTTP
    settings_store: SettingsStore
    prompts: Prompts
    settings: dict[str, Any] = field(default_factory=dict)
    ports: Ports = field(default_factory=Ports)
    _started: bool = False

    @staticmethod
    def thread_key(ref: Any) -> str:
        """Return the canonical key for a Thread reference (Engine parity)."""
        from superagent.core.ids import thread_key as canonical_thread_key

        return canonical_thread_key(ref)

    @classmethod
    def build(
        cls,
        config: Config | None = None,
        *,
        settings: dict[str, Any] | None = None,
        ports: Ports | None = None,
    ) -> Runtime:
        """Wire a Runtime without starting any async task or external call."""
        config, _machine = resolve_deployment(config)
        paths = state_paths(config.state_dir)
        paths.ensure()
        store = Store(paths.database, paths.lock)
        board = HealthBoard()
        effects = Effects(store, board)
        resolved_ports = ports or Ports()
        commands = resolved_ports.commands or SubprocessCommands()
        resolved_ports.commands = commands
        registry = SourceRegistry(commands, board)
        settings = dict(settings or DEFAULT_SETTINGS)
        sources = _OutboxSources(registry, settings.get("sources", {}))
        outbox = Outbox(store, sources, board, effects)
        scope = resolved_ports.scope or _StoreScope()
        assessor = resolved_ports.assessor or _EffectAssessor(effects)
        executor = resolved_ports.executor or _EffectExecutor(effects)
        engine = Engine(
            EngineOptions(
                repo=_StoreRepos(store),
                policy_get=lambda: settings_policy(settings, "runtime"),
                scope=scope,
                assessor=assessor,
                executor=executor,
                outbox=outbox,
                effects=effects,
                prepare_ticket=resolved_ports.prepare_ticket
                or (lambda trigger, gate: _prepare(trigger, config)),
                emit=resolved_ports.emit,
            )
        )
        passes = Passes(_PassRuntime(effects, engine), interval_s=DEFAULT_PASS_INTERVAL_S)
        settings_store = SettingsStore()
        prompts = Prompts()
        panel_api = PanelAPI(
            engine=engine,
            settings=settings_store,
            prompts=prompts,
            board=board,
            registry=registry,
            executor=executor,
        )
        panel = PanelHTTP(panel_api)
        return cls(
            config=config,
            paths=paths,
            store=store,
            board=board,
            effects=effects,
            registry=registry,
            outbox=outbox,
            engine=engine,
            passes=passes,
            panel=panel,
            settings_store=settings_store,
            prompts=prompts,
            settings=settings,
            ports=resolved_ports,
        )

    async def startup(self) -> None:
        """Register handlers, replay in-flight work and start the pass loop."""
        if self._started:
            return
        self.effects.handle("reply", self.outbox.handle_reply)
        self.effects.handle("candidate", _candidate_handler(self))
        self.effects.handle("notice", _notice_handler(self))
        self.effects.handle("launch", _noop_handler)
        self.effects.handle("delivery", _noop_handler)
        self.effects.handle("cancel", _noop_handler)
        await self.passes.start()
        await self.effects.start()
        self._started = True

    async def shutdown(self) -> None:
        """Stop accepting work and drain pending effects for the next boot."""
        if not self._started:
            return
        await self.passes.stop()
        await self.effects.drain()
        self.store.close()
        self._started = False

    def state(self) -> dict[str, Any]:
        """Return a compact process state snapshot (panel + service script)."""
        if self.store._closed:
            return {"started": False, "paused": bool(self.settings.get("paused", False))}
        errors = [name for name, value in self.board.all().items() if value.get("state") == "error"]
        return {
            "started": self._started,
            "paused": bool(self.settings.get("paused", False)),
            "accept_mode": self.settings.get("accept_mode", "observe"),
            "pending_effects": self.effects.pending(),
            "health": self.board.all(),
            "health_error": errors,
            "setup_ok": True,
            "schema_version": self.store.schema_version(),
        }

    async def panel_request(
        self, method: str, path: str, **kwargs: Any
    ) -> tuple[int, dict[str, Any]]:
        """Drive one panel request through the loopback guard."""
        return await self.panel.serve(method, path, **kwargs)


class _StoreScope:
    """SourceScope whose writes are already owned by the Engine."""

    def receive(self, trigger: Any) -> str:
        """Report a Trigger as stored (the Engine owns the write)."""
        return "stored"

    def bind_thread(self, ticket_id: str, thread: Any, own: bool) -> None:
        """No-op: the Engine owns Thread writes."""

    def thread_closed(self, thread: Any, outcome: str, at: str) -> None:
        """No-op: the Engine owns Thread writes."""


class _EffectAssessor:
    """Route assessment requests through the effect queue."""

    def __init__(self, effects: Effects) -> None:
        self.effects = effects

    def request(self, eval_id: str, trigger_ids: list[str]) -> None:
        """Enqueue a candidate assessment."""
        self.effects.enqueue("candidate", eval_id, {"eval_id": eval_id, "triggers": trigger_ids})

    def release(self, eval_id: str) -> None:
        """No-op: candidate delivery marks the Eval as delivered."""


class _EffectExecutor:
    """Route worker commands through the effect queue."""

    def __init__(self, effects: Effects) -> None:
        self.effects = effects

    def start(self, ticket_id: str, assignment_id: str) -> None:
        """Enqueue a launch effect."""
        self.effects.enqueue(
            "launch", ticket_id, {"ticket_id": ticket_id, "assignment_id": assignment_id}
        )

    def deliver(self, ticket_id: str, assignment_id: str, input_ids: list[str]) -> None:
        """Enqueue a delivery effect."""
        self.effects.enqueue(
            "delivery",
            ticket_id,
            {"ticket_id": ticket_id, "assignment_id": assignment_id, "input_ids": input_ids},
        )

    def stop(self, ticket_id: str) -> None:
        """Enqueue a cancel effect."""
        self.effects.enqueue("cancel", ticket_id, {"ticket_id": ticket_id})


def _prepare(trigger: Any, config: Config) -> dict[str, Any]:
    """Build worker metadata for one Ticket."""
    name = f"{trigger.source}-{trigger.id}"[:64]
    return {"name": name, "directory": str(execution_root(config) / name)}


async def _noop_handler(payload: dict[str, Any]) -> None:
    """Placeholder for adapters a host replaces via :class:`Ports`."""
    del payload


def _candidate_handler(runtime: Runtime) -> Callable[[dict[str, Any]], Awaitable[None]]:
    async def handle(payload: dict[str, Any]) -> None:
        eval_id = str(payload.get("eval_id", ""))
        if eval_id:
            runtime.engine.candidate_delivered(eval_id)

    return handle


def _notice_handler(runtime: Runtime) -> Callable[[dict[str, Any]], Awaitable[None]]:
    async def handle(payload: dict[str, Any]) -> None:
        del payload

    return handle


async def run(config: Config | None = None, *, stop: asyncio.Event | None = None) -> Runtime:
    """Build and run a Runtime until ``stop`` is set (used by ``__main__``)."""
    runtime = Runtime.build(config)
    await runtime.startup()
    stop = stop or asyncio.Event()
    try:
        await stop.wait()
    finally:
        await runtime.shutdown()
    return runtime


def main(argv: list[str] | None = None) -> int:
    """CLI entry point for ``python -m superagent``."""
    import argparse

    parser = argparse.ArgumentParser(prog="superagent", description="Superagent side process")
    parser.add_argument("--state-dir", default="", help="override DSH_SUPERAGENT_STATE_DIR")
    parser.add_argument("--port", type=int, default=0, help="override the panel port")
    args = parser.parse_args(argv)
    config = Config.from_env()
    if args.state_dir:
        object.__setattr__(config, "state_dir", args.state_dir)
    if args.port:
        object.__setattr__(config, "port", args.port)

    stop = asyncio.Event()

    def _on_signal(*_sig: object) -> None:
        stop.set()

    async def _serve() -> None:
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, _on_signal)
            except (RuntimeError, NotImplementedError):
                pass
        await run(config, stop=stop)

    asyncio.run(_serve())
    return 0


__all__ = ["Ports", "Runtime", "main", "run"]
