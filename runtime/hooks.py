"""Process-level callback registry for dependency inversion (leaf module).

Some layers must react to events owned by other layers without importing
them: the agent layer needs the server layer's auto-turn trigger and its live
WS task table, but ``agent/**`` reaching up into ``server/**`` is the
``agent <-> server`` import cycle this registry exists to break. Owners
register their callables here at assembly time (server boot); consumers
resolve them at call time and degrade to a no-op when nothing is registered
(evals, unit tests, any process that never assembled a server).

Leaf module by contract: standard library only, no project imports, and it
never calls anything itself. Registration is last-writer-wins, so repeated
assembly is idempotent.

Hook contracts (registration sites must provide these signatures; the
``agent/**`` consumers depend on them):

``MAYBE_TRIGGER_AUTO_TURN`` (``"maybe_trigger_auto_turn"``)
    ``async (session_key: str, injection: HumanMessage) -> AutoTurnResult``.
    Fire-and-forget idle auto-turn trigger; callers ``await`` the returned
    awaitable and treat a ``"triggered"`` outcome as delivered. Missing hook
    -> consumers treat the turn as not triggered and fall back to their own
    persistence path.

``AUTO_TURN_MODULE`` (``"auto_turn_module"``)
    ``() -> module``: getter returning the auto-turn module for read-only
    access to its ``_INFLIGHT`` / ``_INFLIGHT_LOCK`` registry. Missing hook
    -> the ``auto_turn_inflight`` session signal reports not-live.

``WS_ACTIVE_TASKS`` (``"ws_active_tasks"``)
    ``() -> dict[str, asyncio.Task]``: getter returning the live per-session
    WS stream task table (owned and mutated in place by the transport layer;
    callers only read it). Missing hook -> consumers see an empty table.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

__all__ = [
    "AUTO_TURN_MODULE",
    "MAYBE_TRIGGER_AUTO_TURN",
    "WS_ACTIVE_TASKS",
    "clear",
    "register",
    "resolve",
    "unregister",
]

MAYBE_TRIGGER_AUTO_TURN = "maybe_trigger_auto_turn"
AUTO_TURN_MODULE = "auto_turn_module"
WS_ACTIVE_TASKS = "ws_active_tasks"

_registry: dict[str, Callable[..., Any]] = {}


def register(name: str, fn: Callable[..., Any]) -> None:
    """Bind ``name`` to ``fn`` (last registration wins; idempotent re-assembly)."""
    _registry[name] = fn


def resolve(name: str) -> Callable[..., Any] | None:
    """Return the callable bound to ``name``, or ``None`` when unregistered."""
    return _registry.get(name)


def unregister(name: str) -> None:
    """Drop ``name`` if present (no-op otherwise); for test/teardown isolation."""
    _registry.pop(name, None)


def clear() -> None:
    """Drop every registration (test isolation / process teardown)."""
    _registry.clear()
