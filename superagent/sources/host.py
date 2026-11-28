"""Source host and context (prototype ``src/sources/host.ts``).

Sources receive this narrow surface instead of the Runtime, so they can be
unit-tested with a fake context and cannot reach Engine internals.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

from superagent.core.model import Receipt, Trigger
from superagent.infra.commands import Commands
from superagent.infra.health import HealthBoard


@dataclass(frozen=True)
class HealthState:
    """One health report from a Source."""

    name: str
    state: str
    detail: str = ""


@dataclass
class SourceContext:
    """Capabilities handed to one Source instance."""

    commands: Commands
    board: HealthBoard
    ready: bool = True
    configured: Callable[[], bool] = lambda: True
    config: Callable[[str], dict[str, Any] | None] = lambda _kind: None
    receive: Callable[[Trigger], str] = lambda _trigger: "filtered"
    atomic: Any = None
    state: Any = None
    threads: Any = None
    tickets: Any = None
    notify: Callable[[Receipt], None] = lambda _message: None
    report: Callable[[str, HealthState], None] = lambda _name, _state: None
    runs: list[str] = field(default_factory=list)

    def emit(self, trigger: Trigger) -> str:
        """Record and forward a Trigger to the Engine."""
        self.runs.append(trigger.id)
        return self.receive(trigger)


class SourceHost(Protocol):
    """The even narrower plugin-facing source host."""

    def context(self, kind_name: str) -> SourceContext: ...


def default_context(
    commands: Commands,
    board: HealthBoard,
    receive: Callable[[Trigger], str],
    config: Callable[[str], dict[str, Any] | None] | None = None,
) -> SourceContext:
    """Build a context with the mandatory wiring only."""
    return SourceContext(
        commands=commands,
        board=board,
        receive=receive,
        config=config or (lambda _kind: None),
    )


__all__ = ["HealthState", "SourceContext", "SourceHost", "default_context"]
