"""Engine options and shared part base (prototype ``src/core/part.ts``)."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from superagent.core.ports import (
    Assessor,
    DomainEvent,
    EffectPort,
    Executor,
    OutboxPort,
    PolicyPort,
    Repositories,
    SourceScope,
)
from superagent.core.model import Trigger


@dataclass
class EngineOptions:
    """Everything the Engine needs from its host."""

    repo: Repositories
    policy_get: Callable[[], PolicyPort]
    scope: SourceScope
    assessor: Assessor
    executor: Executor
    outbox: OutboxPort
    effects: EffectPort
    prepare_ticket: Callable[[Trigger, Any], dict[str, Any]]
    now: Callable[[], str] | None = None
    emit: Callable[[DomainEvent], None] | None = None


class EnginePart:
    """Base class for the five one-way-dependent Engine parts."""

    def __init__(self, engine: Any) -> None:
        self.engine = engine
