"""Task 2 — detect_state extension: ``hitl_pending`` + auto-turn ``_INFLIGHT`` signals.

Frozen priority contract (documented in the ``detect_state`` docstring and
asserted here):

    ws_task > answering > hitl_pending > auto_turn_inflight > idle

New signals (both must yield NON-idle busy results):

1. ``hitl_pending`` — per-session registry owned by
   ``agent.tools.subagent.registry.session_state`` (mutated ONLY via
   ``set_hitl_pending(session_id, value)``; Task 7 wires the WS-layer
   set/clear calls). Fixes the HITL wait being misreported as idle:
   during ``hitl_request`` waits the WS task has been popped
   (messages.py:114-118) and ``answering`` is cleared (:548), yet the
   graph is suspended awaiting ``hitl_response``.

2. ``auto_turn_inflight`` — membership of the module-level
   ``server.service.auto_turn._INFLIGHT`` dict (bare id -> runner task,
   "live" = present AND not done, same shape as the ws_task signal).
   Narrows the TOCTOU window: the answering flag is only set at
   messages.py:273 AFTER the heavy ``built_agent(force_rebuild=True)``
   rebuild (:220), while ``_INFLIGHT`` is registered at auto-turn
   dispatch time.

The pre-existing 3-level behavior (ws_task > answering > idle) MUST NOT
change (guardrail G5) — asserted by the "existing signals still win" and
"all clear -> idle" cases.

All signal sources are faked/controlled per test — no real WS server, no
robyn, no LLM. The one real-wiring test imports
``server.service.auto_turn`` directly (proven hermetic by
tests/unit/test_auto_turn.py) to prove the lazy-import seam resolves.
"""

import threading
from types import SimpleNamespace

import pytest

from agent.tools.subagent.registry import session_state as ss
from agent.tools.subagent.registry.session_keys import SESSION_KEY_PREFIX
from agent.tools.subagent.registry.session_state import (
    detect_state,
    is_session_busy,
    set_hitl_pending,
)

_SESSION_STATE_MODULE = "agent.tools.subagent.registry.session_state"

pytestmark = pytest.mark.unit


class FakeStateRegister:
    """Minimal stand-in for runtime.state_register.state_register_mem."""

    def __init__(self) -> None:
        self.answering: dict[str, object] = {}

    def get_state(self, session_id: str, key: str, default: object = None) -> object:
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


@pytest.fixture(autouse=True)
def _clean_hitl_registry():
    """Keep the module-level HITL registry empty around every test."""
    ss._HITL_PENDING.clear()
    yield
    ss._HITL_PENDING.clear()


class FakeAutoTurnModule:
    """Stand-in for server.service.auto_turn exposing the _INFLIGHT shape."""

    def __init__(self) -> None:
        self._INFLIGHT: dict[str, object] = {}
        self._INFLIGHT_LOCK = threading.Lock()


@pytest.fixture
def auto_turn_mod(monkeypatch) -> FakeAutoTurnModule:
    mod = FakeAutoTurnModule()
    monkeypatch.setattr(f"{_SESSION_STATE_MODULE}._get_auto_turn_module", lambda: mod)
    return mod


def make_live_task() -> SimpleNamespace:
    return SimpleNamespace(done=lambda: False)


def make_done_task() -> SimpleNamespace:
    return SimpleNamespace(done=lambda: True)


class TestHitlPendingSignal:
    def test_hitl_pending_reports_busy(self, active_tasks, state_register):
        set_hitl_pending("abc", True)
        state = detect_state("abc")
        assert state.busy is True
        assert state.reason == "hitl_pending"
        assert state.session_id == "abc"

    def test_set_hitl_pending_false_clears(self, active_tasks, state_register):
        set_hitl_pending("abc", True)
        assert is_session_busy("abc") is True
        set_hitl_pending("abc", False)
        state = detect_state("abc")
        assert state.busy is False
        assert state.reason == "idle"

    def test_hitl_pending_normalized_like_other_signals(self, active_tasks, state_register):
        # Announce-side prefixed key must hit the same bare-id registry entry.
        set_hitl_pending(f"{SESSION_KEY_PREFIX}abc", True)
        assert is_session_busy("abc") is True
        assert detect_state(f"{SESSION_KEY_PREFIX}abc").reason == "hitl_pending"

    def test_hitl_pending_scoped_per_session(self, active_tasks, state_register):
        set_hitl_pending("abc", True)
        assert is_session_busy("other-session") is False

    def test_detect_does_not_mutate_hitl_registry(self, active_tasks, state_register):
        set_hitl_pending("abc", True)
        snapshot = set(ss._HITL_PENDING)
        detect_state("abc")
        detect_state(f"{SESSION_KEY_PREFIX}abc")
        detect_state("unknown")
        assert ss._HITL_PENDING == snapshot


class TestAutoTurnInflightSignal:
    def test_inflight_reports_busy(self, active_tasks, state_register, auto_turn_mod):
        auto_turn_mod._INFLIGHT["abc"] = make_live_task()
        state = detect_state("abc")
        assert state.busy is True
        assert state.reason == "auto_turn_inflight"
        assert state.session_id == "abc"

    def test_done_inflight_task_not_busy(self, active_tasks, state_register, auto_turn_mod):
        # Stale entry: same "live" semantics as the ws_task signal.
        auto_turn_mod._INFLIGHT["abc"] = make_done_task()
        state = detect_state("abc")
        assert state.busy is False
        assert state.reason == "idle"

    def test_real_auto_turn_module_wiring(self, active_tasks, state_register, monkeypatch):
        """The lazy import seam resolves to the REAL auto_turn module (the
        fake-module tests above prove precedence; this one proves wiring)."""
        from server.service import auto_turn as real_auto_turn

        monkeypatch.setattr(
            f"{_SESSION_STATE_MODULE}._get_auto_turn_module", lambda: real_auto_turn
        )
        real_auto_turn._INFLIGHT["abc"] = make_live_task()
        try:
            state = detect_state("abc")
            assert state.busy is True
            assert state.reason == "auto_turn_inflight"
        finally:
            real_auto_turn._INFLIGHT.pop("abc", None)


class TestPriorityOrder:
    def test_hitl_pending_wins_over_auto_turn_inflight(
        self, active_tasks, state_register, auto_turn_mod
    ):
        # HITL wait is the more specific state while an auto turn may also be
        # registered; hitl_pending is more specific (plan Task 2 rationale).
        set_hitl_pending("abc", True)
        auto_turn_mod._INFLIGHT["abc"] = make_live_task()
        state = detect_state("abc")
        assert state.busy is True
        assert state.reason == "hitl_pending"

    def test_ws_task_still_wins_over_hitl_pending(self, active_tasks, state_register):
        # G5: pre-existing priority unchanged — ws_task beats the new signal.
        set_hitl_pending("abc", True)
        active_tasks["abc"] = make_live_task()
        state = detect_state("abc")
        assert state.busy is True
        assert state.reason == "ws_task"

    def test_answering_still_wins_over_hitl_pending(self, active_tasks, state_register):
        set_hitl_pending("abc", True)
        state_register.answering["abc"] = True
        state = detect_state("abc")
        assert state.busy is True
        assert state.reason == "answering"

    def test_answering_wins_over_auto_turn_inflight(
        self, active_tasks, state_register, auto_turn_mod
    ):
        state_register.answering["abc"] = True
        auto_turn_mod._INFLIGHT["abc"] = make_live_task()
        state = detect_state("abc")
        assert state.busy is True
        assert state.reason == "answering"

    def test_ws_task_still_wins_over_auto_turn_inflight(
        self, active_tasks, state_register, auto_turn_mod
    ):
        active_tasks["abc"] = make_live_task()
        auto_turn_mod._INFLIGHT["abc"] = make_live_task()
        assert detect_state("abc").reason == "ws_task"


class TestIdleUnchanged:
    def test_all_clear_still_idle(self, active_tasks, state_register, auto_turn_mod):
        state = detect_state("abc")
        assert state.busy is False
        assert state.reason == "idle"
        assert state.session_id == "abc"

    def test_stale_done_ws_task_falls_through_to_hitl_pending(
        self, active_tasks, state_register
    ):
        # Existing fall-through behavior extended one level: done ws task ->
        # answering (absent) -> hitl_pending.
        active_tasks["abc"] = make_done_task()
        set_hitl_pending("abc", True)
        state = detect_state("abc")
        assert state.busy is True
        assert state.reason == "hitl_pending"
