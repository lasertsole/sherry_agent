"""Session purge drops the module-level per-session dictionaries (audit M18).

The guardrail lock table, the interaction lock tables and shadow-git's turn
bookkeeping are keyed by session id and are not part of any register, so a
deleted session used to leave one entry per structure behind for the process
lifetime. ``purge_session_data`` calls each module's clear helper.
"""

from __future__ import annotations

import asyncio
import threading

import pytest

import agent.middlewares.tool_guardrails.core as guardrails
import agent.middlewares.tool_resource_lock as resource_lock
import agent.tools.pub_base.shadow_git as shadow_git

pytestmark = [pytest.mark.unit, pytest.mark.timeout(30)]

SESSION = "sess-cleanup"


def test_guardrail_lock_is_dropped():
    with guardrails._STATE_LOCKS_GUARD:
        guardrails._STATE_LOCKS[SESSION] = threading.Lock()

    assert guardrails.clear_session_state(SESSION) is True
    assert SESSION not in guardrails._STATE_LOCKS
    assert guardrails.clear_session_state(SESSION) is False


def test_interaction_locks_are_dropped_on_every_loop():
    loop = asyncio.new_event_loop()
    try:
        with resource_lock._LOCKS_GUARD:
            resource_lock._SYNC_LOCKS[f"interaction:{SESSION}"] = threading.Lock()
            resource_lock._ASYNC_LOCKS[loop] = {f"interaction:{SESSION}": asyncio.Lock()}

        assert resource_lock.clear_session_locks(SESSION) == 2

        assert f"interaction:{SESSION}" not in resource_lock._SYNC_LOCKS
        assert f"interaction:{SESSION}" not in resource_lock._ASYNC_LOCKS[loop]
        assert resource_lock.clear_session_locks(SESSION) == 0
    finally:
        loop.close()


def test_shadow_git_bookkeeping_is_dropped():
    with shadow_git._floors_lock:
        shadow_git._turn_counts[SESSION] = 3
        shadow_git._dedup.add((SESSION, "root-key"))
        shadow_git._dedup.add(("other", "root-key"))
        shadow_git._floors[("root-key", SESSION)] = {"turn": 3, "tip": "abc"}
        shadow_git._floors[("root-key", "other")] = {"turn": 1, "tip": "def"}

    assert shadow_git.clear_session_state(SESSION) == 3

    assert SESSION not in shadow_git._turn_counts
    assert (SESSION, "root-key") not in shadow_git._dedup
    assert ("root-key", SESSION) not in shadow_git._floors
    # Another session's entries are untouched.
    assert ("other", "root-key") in shadow_git._dedup
    assert ("root-key", "other") in shadow_git._floors
