"""Loop-safe wake primitives for event-driven waits (leaf module: stdlib only).

Three polling loops were replaced by "wait for a notification, then re-check
state" loops: the TaskFlow ``wait_all`` tool, the subagent sweeper, and the
auto-turn self-idle wait. They all need the same two properties:

1. **No lost wakeups.** A waiter re-arms the event BEFORE it re-checks the
   state and then waits. A notification that lands between the re-check and the
   wait leaves the event set, so ``wait`` returns at once; a notification that
   lands before the re-check is observed by the re-check itself. Both orders
   are safe because the check is synchronous (there is no await between
   ``rearm`` and the check).
2. **Thread-safe wake.** Registry state is mutated from the event loop in
   practice, but the registry surface is process-level and reachable from
   threads (persist/restore and tests). ``wake`` therefore captures the loop a
   waiter actually awaits on and hops with ``call_soon_threadsafe`` when the
   caller is on another thread (or outside a loop entirely).

A notification is a HINT, never a promise: waiters must re-read the state they
care about after every wake (and after every fallback timeout). Publication is
fail-open — a broken/closed subscriber is dropped by ``call_soon_threadsafe``'s
``RuntimeError`` and the fallback poll still observes the state.
"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Iterable

__all__ = ["WakeEvent", "WakeRegistry", "WakeSubscription"]


class WakeEvent:
    """One ``asyncio.Event`` with a loop binding and thread-safe ``wake``.

    The loop is captured lazily: construction may happen before (or outside) a
    running loop, while ``wait`` always runs inside one.
    """

    __slots__ = ("_event", "_loop")

    def __init__(self) -> None:
        self._event = asyncio.Event()
        self._loop: asyncio.AbstractEventLoop | None = None

    def _bind_loop(self) -> None:
        if self._loop is None:
            try:
                self._loop = asyncio.get_running_loop()
            except RuntimeError:
                # Constructed outside a loop; wait() binds it when it runs.
                pass

    def rearm(self) -> None:
        """Clear the event so the next wait suspends again."""
        self._event.clear()

    def is_set(self) -> bool:
        """Whether a wake is pending (not yet consumed by a re-arm)."""
        return self._event.is_set()

    def wake(self) -> None:
        """Set the event, hopping to the waiter's loop when called cross-thread."""
        loop = self._loop
        if loop is None:
            # No waiter ever awaited this event: a plain set is enough and is
            # observed by a later is_set()/wait() on whatever loop runs it.
            self._event.set()
            return
        try:
            running: asyncio.AbstractEventLoop | None = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is loop:
            self._event.set()
            return
        try:
            loop.call_soon_threadsafe(self._event.set)
        except RuntimeError:
            # The waiter's loop is closed (its task is gone); nothing to wake.
            pass

    async def wait(self, timeout: float) -> bool:
        """Await a wake for at most ``timeout`` seconds.

        :returns: True when woken, False when the timeout elapsed first.
        """
        self._bind_loop()
        if self._event.is_set():
            return True
        try:
            await asyncio.wait_for(self._event.wait(), timeout=max(0.0, float(timeout)))
            return True
        except TimeoutError:
            return False


class WakeSubscription:
    """One subscriber: an optional KEY FILTER plus one wake event."""

    __slots__ = ("_event", "_keys")

    def __init__(self, keys: frozenset[str] | None = None) -> None:
        # None = every notification; an empty set = nothing matches.
        self._keys = keys
        self._event = WakeEvent()

    def matches(self, key: str) -> bool:
        """Whether a notification for ``key`` is relevant to this subscriber."""
        return self._keys is None or key in self._keys

    def rearm(self) -> None:
        """Clear the pending wake (call before re-checking state)."""
        self._event.rearm()

    def is_set(self) -> bool:
        """Whether a wake is pending."""
        return self._event.is_set()

    def wake(self) -> None:
        """Set this subscription's wake event."""
        self._event.wake()

    async def wait(self, timeout: float) -> bool:
        """Wait up to ``timeout`` seconds for a matching notification."""
        return await self._event.wait(timeout)


class WakeRegistry:
    """Thread-safe registry of wake subscriptions, optionally key-filtered."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._subscriptions: dict[int, WakeSubscription] = {}

    def subscribe(self, keys: Iterable[str] | None = None) -> WakeSubscription:
        """Register a subscriber: ``keys=None`` receives every notification."""
        subscription = WakeSubscription(None if keys is None else frozenset(keys))
        with self._lock:
            self._subscriptions[id(subscription)] = subscription
        return subscription

    def unsubscribe(self, subscription: WakeSubscription) -> None:
        """Drop a subscriber (idempotent)."""
        with self._lock:
            self._subscriptions.pop(id(subscription), None)

    def subscription_count(self) -> int:
        """Number of live subscriptions (test/diagnostic seam)."""
        with self._lock:
            return len(self._subscriptions)

    def notify(self, key: str) -> int:
        """Wake every subscriber that matches ``key``; return how many."""
        with self._lock:
            subscriptions = list(self._subscriptions.values())
        woken = 0
        for subscription in subscriptions:
            if subscription.matches(key):
                subscription.wake()
                woken += 1
        return woken

    def notify_all(self) -> int:
        """Wake every subscriber regardless of key; return how many."""
        with self._lock:
            subscriptions = list(self._subscriptions.values())
        for subscription in subscriptions:
            subscription.wake()
        return len(subscriptions)
