"""Unit tests for agent.tools.subagent.registry.session_state.

Detection contract (frozen for the delivery router — Task 9 consumer):
    is_session_busy(session_key: str) -> bool
    detect_state(session_key: str) -> SessionState(busy, reason, session_id)

A parent session is BUSY iff it has a live WS stream task in
``server.trigger.ws.messages._active_tasks`` (dict keyed by the BARE session
id; "live" = task is not done) OR ``state_register_mem``'s ``answering`` flag
is truthy for the bare id.

Both signal sources are faked here via monkeypatched accessors — no real WS
server, no robyn/channel imports. Keys arrive in announce-side format
``agent:main:session:{id}`` and are normalized to the bare id via Task 1's
frozen ``normalize_session_key`` contract before lookup.

Detection is READ-ONLY: tests assert neither faked structure is mutated.
"""

import asyncio
import contextlib
from types import SimpleNamespace

import pytest

from agent.tools.subagent.registry.session_keys import (
    SESSION_KEY_PREFIX,
    normalize_session_key,
)
from agent.tools.subagent.registry.session_state import detect_state, is_session_busy

_SESSION_STATE_MODULE = "agent.tools.subagent.registry.session_state"


class FakeStateRegister:
    """Minimal stand-in for runtime.state_register.state_register_mem."""

    def __init__(self, answering: dict[str, object] | None = None):
        self.answering = answering or {}
        self.get_state_calls: list[tuple[str, str]] = []

    def get_state(self, session_id: str, key: str, default: object = None) -> object:
        self.get_state_calls.append((session_id, key))
        return self.answering.get(session_id, default)


@pytest.fixture
def active_tasks(monkeypatch) -> dict[str, object]:
    """Fake ``_active_tasks`` dict (keyed by bare session id)."""
    tasks: dict[str, object] = {}
    monkeypatch.setattr(f"{_SESSION_STATE_MODULE}._get_active_tasks", lambda: tasks)
    return tasks


@pytest.fixture
def state_register(monkeypatch) -> FakeStateRegister:
    register = FakeStateRegister()
    monkeypatch.setattr(f"{_SESSION_STATE_MODULE}._get_state_register", lambda: register)
    return register


def make_live_task() -> SimpleNamespace:
    """Pending (not started) task shape: done() -> False."""
    return SimpleNamespace(done=lambda: False)


def make_done_task() -> SimpleNamespace:
    return SimpleNamespace(done=lambda: True)


class TestBusyByWsTask:
    def test_live_task_busy_bare_key(self, active_tasks, state_register):
        active_tasks["abc"] = make_live_task()
        assert is_session_busy("abc") is True

    def test_reason_reports_ws_task(self, active_tasks, state_register):
        active_tasks["abc"] = make_live_task()
        state = detect_state("abc")
        assert state.busy is True
        assert state.reason == "ws_task"
        assert state.session_id == "abc"

    def test_done_task_is_not_busy(self, active_tasks, state_register):
        # Stale entry: the real finally-block pops the task itself, so a done
        # task in the dict must NOT report busy (falls through to answering).
        active_tasks["abc"] = make_done_task()
        assert is_session_busy("abc") is False

    def test_done_task_falls_through_to_answering(self, active_tasks, state_register):
        active_tasks["abc"] = make_done_task()
        state_register.answering["abc"] = True
        state = detect_state("abc")
        assert state.busy is True
        assert state.reason == "answering"

    def test_other_sessions_task_does_not_leak(self, active_tasks, state_register):
        active_tasks["other-session"] = make_live_task()
        assert is_session_busy("abc") is False


class TestBusyByAnsweringFlag:
    def test_answering_true_busy(self, active_tasks, state_register):
        state_register.answering["abc"] = True
        assert is_session_busy("abc") is True

    def test_reason_reports_answering(self, active_tasks, state_register):
        state_register.answering["abc"] = True
        state = detect_state("abc")
        assert state.busy is True
        assert state.reason == "answering"
        assert state.session_id == "abc"

    def test_answering_false_is_idle(self, active_tasks, state_register):
        state_register.answering["abc"] = False
        assert is_session_busy("abc") is False

    def test_missing_answering_defaults_idle(self, active_tasks, state_register):
        assert is_session_busy("abc") is False


class TestIdle:
    def test_no_signals_idle(self, active_tasks, state_register):
        state = detect_state("abc")
        assert state.busy is False
        assert state.reason == "idle"
        assert state.session_id == "abc"

    def test_ws_task_takes_precedence_over_answering(self, active_tasks, state_register):
        active_tasks["abc"] = make_live_task()
        state_register.answering["abc"] = True
        state = detect_state("abc")
        assert state.busy is True
        assert state.reason == "ws_task"


class TestPrefixNormalization:
    """Announce-side keys carry ``agent:main:session:`` — must hit the same
    bare-id entries as the service side."""

    def test_prefixed_key_matches_bare_task_entry(self, active_tasks, state_register):
        active_tasks["abc"] = make_live_task()
        assert is_session_busy(f"{SESSION_KEY_PREFIX}abc") is True

    def test_prefixed_key_matches_bare_answering_entry(self, active_tasks, state_register):
        state_register.answering["abc"] = True
        state = detect_state(f"{SESSION_KEY_PREFIX}abc")
        assert state.busy is True
        assert state.reason == "answering"
        assert state.session_id == "abc"

    def test_prefixed_and_bare_same_result(self, active_tasks, state_register):
        active_tasks["abc"] = make_live_task()
        assert detect_state("abc") == detect_state(f"{SESSION_KEY_PREFIX}abc")

    def test_session_id_field_is_bare(self, active_tasks, state_register):
        state = detect_state(f"{SESSION_KEY_PREFIX}abc")
        assert state.session_id == normalize_session_key(f"{SESSION_KEY_PREFIX}abc") == "abc"


class TestReadOnly:
    def test_active_tasks_not_mutated(self, active_tasks, state_register):
        active_tasks["abc"] = make_live_task()
        snapshot = dict(active_tasks)
        is_session_busy("abc")
        is_session_busy(f"{SESSION_KEY_PREFIX}abc")
        is_session_busy("unknown")
        assert active_tasks == snapshot
        assert active_tasks is not None

    def test_state_register_only_read(self, active_tasks, state_register):
        state_register.answering["abc"] = True
        is_session_busy("abc")
        # exactly one get_state read, no writes possible via public API
        assert state_register.get_state_calls == [("abc", "answering")]


class TestRealTaskShapes:
    """The real ``_active_tasks`` values are genuine ``asyncio.Task`` objects;
    verify the detector's ``not task.done()`` semantics against real tasks."""

    def test_real_asyncio_task_lifecycle(self, active_tasks, state_register):
        async def spin():
            await asyncio.sleep(3600)

        async def scenario():
            task = asyncio.create_task(spin())
            active_tasks["abc"] = task
            # Pending task: live -> busy.
            assert task.done() is False
            assert is_session_busy("abc") is True
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
            # Done task: stale entry must not report busy.
            assert task.done() is True
            assert is_session_busy("abc") is False

        asyncio.run(scenario())
