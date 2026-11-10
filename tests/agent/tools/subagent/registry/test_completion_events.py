"""Unit tests for the subagent terminal-transition notification hub.

The hub is the event half of the wait_all / sweeper de-polling: every terminal
run transition publishes one keyed notification, waiters subscribe with the
child session keys they care about, and EVERY consumer keeps its bounded
fallback poll (a missed notification is never a lost result).

Coverage:
(a) keyed subscription wakes only for its own child session key
(b) keys=None receives every notification (the sweeper's scope)
(c) notify_all wakes keyed and unscoped subscribers alike (registry clear)
(d) unsubscribe stops delivery, and publication is fail-open with no subscribers
"""

import asyncio
import threading

import pytest

from agent.tools.subagent.registry import completion_events

pytestmark = [pytest.mark.unit]


@pytest.mark.asyncio
async def test_keyed_subscription_wakes_only_for_its_keys():
    subscription = completion_events.subscribe_run_terminal(["child-a"])
    try:
        # Given a waiter scoped to child-a
        # When an unrelated child settles
        completion_events.notify_run_terminal("child-b", run_id="run-b")
        # Then the waiter is NOT woken (the fallback would have to catch it)
        assert await subscription.wait(0.05) is False

        # When its own child settles
        completion_events.notify_run_terminal("child-a", run_id="run-a")
        # Then it wakes immediately
        assert await subscription.wait(0.05) is True
    finally:
        completion_events.unsubscribe_run_terminal(subscription)


@pytest.mark.asyncio
async def test_unscoped_subscription_receives_every_notification():
    subscription = completion_events.subscribe_run_terminal(None)
    try:
        completion_events.notify_run_terminal("any-child", run_id="run-x")
        assert await subscription.wait(0.05) is True
    finally:
        completion_events.unsubscribe_run_terminal(subscription)


@pytest.mark.asyncio
async def test_notify_all_wakes_keyed_and_unscoped_subscribers():
    keyed = completion_events.subscribe_run_terminal(["child-a"])
    unscoped = completion_events.subscribe_run_terminal(None)
    try:
        woken = completion_events.notify_all_terminal()
        assert woken >= 2
        assert await keyed.wait(0.05) is True
        assert await unscoped.wait(0.05) is True
    finally:
        completion_events.unsubscribe_run_terminal(keyed)
        completion_events.unsubscribe_run_terminal(unscoped)


@pytest.mark.asyncio
async def test_unsubscribe_stops_delivery_and_publication_is_fail_open():
    subscription = completion_events.subscribe_run_terminal(["child-a"])
    completion_events.unsubscribe_run_terminal(subscription)

    # Given the only subscriber is gone
    before = completion_events.active_subscription_count()
    # When a transition publishes into an empty hub
    assert completion_events.notify_run_terminal("child-a") == 0
    # Then nothing raises and the closed subscription stays silent.
    assert completion_events.active_subscription_count() == before
    assert await subscription.wait(0.01) is False


@pytest.mark.asyncio
async def test_cross_thread_publication_wakes_the_waiter():
    """A registry write from a worker thread hops onto the waiter's loop."""
    subscription = completion_events.subscribe_run_terminal(["child-thread"])
    try:
        waiter = asyncio.create_task(subscription.wait(2.0))
        await asyncio.sleep(0.02)  # let the waiter bind its loop and suspend

        publisher = threading.Thread(
            target=lambda: completion_events.notify_run_terminal("child-thread", run_id="r-t")
        )
        publisher.start()
        publisher.join(timeout=2.0)

        assert await asyncio.wait_for(waiter, timeout=2.0) is True
    finally:
        completion_events.unsubscribe_run_terminal(subscription)


@pytest.mark.asyncio
async def test_registry_terminal_transition_publishes():
    """End-to-end seam: memory.set_run publishes exactly once per terminal transition."""
    from agent.tools.subagent.registry import memory
    from agent.tools.subagent.types.registry import (
        ExecutionState,
        ExecutionStatus,
        RunOutcome,
        RunOutcomeStatus,
        SubagentRunRecord,
    )

    run = SubagentRunRecord(
        run_id="run-pub",
        task_run_id="run-pub",
        child_session_key="child-pub",
        requester_session_key="agent:main:session:req",
        task="task",
    )
    subscription = completion_events.subscribe_run_terminal(["child-pub"])
    memory.delete("run-pub")
    try:
        memory.set_run(run)  # still live: no notification
        assert await subscription.wait(0.01) is False

        terminal = run.model_copy(
            update={
                "execution": ExecutionState(
                    status=ExecutionStatus.TERMINAL,
                    started_at=run.execution.started_at,
                    ended_at=run.execution.started_at,
                    outcome=RunOutcome(status=RunOutcomeStatus.OK),
                ),
                "ended_reason": "complete",
            }
        )
        memory.set_run(terminal)
        assert await subscription.wait(0.05) is True

        # A second write of the same terminal record does not re-publish.
        subscription.rearm()
        memory.set_run(terminal)
        assert await subscription.wait(0.01) is False
    finally:
        completion_events.unsubscribe_run_terminal(subscription)
        memory.delete("run-pub")
