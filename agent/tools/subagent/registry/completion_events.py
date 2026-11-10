"""Terminal-transition notification hub for subagent runs (leaf module).

Two consumers used to poll the registry for a run reaching TERMINAL:

- ``taskflow_wait_all`` polls only the child sessions recorded on its flow's
  dispatched steps (a flow/children-scoped wait);
- the registry sweeper polls the whole registry on its periodic cycle.

Both now subscribe here and are woken the instant a run turns terminal. This is
deliberately a SIBLING of :mod:`agent.tools.subagent.registry.yield_events`:
that registry answers "have ALL of this requester's children settled" and
notifies a yielding parent, while this one is a pure keyed fan-out (one child
session key -> every interested waiter). Reusing it would couple the wait_all
tool to the yield state machine and its settle-wake batching.

Publication lives in :mod:`agent.tools.subagent.registry.memory` — the single
in-memory write door every terminal transition passes through — so a new
terminalization path cannot silently forget to notify. Publication is
FAIL-OPEN: a subscriber or import problem never breaks a registry write, and a
missed notification only costs the caller one fallback poll (every consumer
keeps its bounded fallback wait).
"""

from __future__ import annotations

from collections.abc import Iterable

from loguru import logger

from runtime.event_wait import WakeRegistry, WakeSubscription

__all__ = [
    "active_subscription_count",
    "notify_all_terminal",
    "notify_run_terminal",
    "subscribe_run_terminal",
    "unsubscribe_run_terminal",
]

_hub = WakeRegistry()


def subscribe_run_terminal(
    child_session_keys: Iterable[str] | None = None,
) -> WakeSubscription:
    """Subscribe to terminal transitions of the given child session keys.

    ``None`` subscribes to EVERY terminal transition (the sweeper's scope).
    An iterable subscribes to just those child session keys (the flow-scoped
    ``wait_all`` scope). The caller owns the returned subscription and must
    pass it to :func:`unsubscribe_run_terminal` when done.
    """
    keys = None if child_session_keys is None else tuple(str(k) for k in child_session_keys)
    return _hub.subscribe(keys)


def unsubscribe_run_terminal(subscription: WakeSubscription) -> None:
    """Drop a subscription (idempotent; safe after the registry already cleared)."""
    _hub.unsubscribe(subscription)


def notify_run_terminal(
    child_session_key: str, run_id: str | None = None, *, cleared: bool = False
) -> int:
    """Publish one settled-run fact; return how many subscriptions were woken.

    ``cleared=True`` marks a registry-wide clear (every run is now unknown,
    which ``wait_all`` treats as settled). Publication never raises.
    """
    try:
        if cleared:
            return _hub.notify_all()
        return _hub.notify(str(child_session_key or ""))
    except Exception:  # noqa: BLE001 - a notification is a hint, never a failure
        logger.debug(
            "completion_events: publish failed for child={} run_id={}",
            child_session_key,
            run_id,
            exc_info=True,
        )
        return 0


def notify_all_terminal() -> int:
    """Wake every subscriber (registry cleared: no run is live any more)."""
    return notify_run_terminal("", cleared=True)


def active_subscription_count() -> int:
    """Number of live subscriptions (test/diagnostic seam)."""
    return _hub.subscription_count()
