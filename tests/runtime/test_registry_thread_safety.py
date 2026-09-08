"""TDD tests for audit issue #13 — runtime/ register cluster thread safety.

Audit findings being pinned by these tests:

* ``runtime/core.py`` — ``Register.__new__`` mutates the class-level
  ``_instances`` dict without a lock (two threads can each build an
  instance); ``clear_all_register_sessions`` iterates subclasses unlocked.
* ``runtime/state_register.py`` — ``StateRegisterMeM._states`` mutated
  unlocked; ``get_all_states`` returns the LIVE dict (callers can mutate
  shared state through it).
* ``runtime/relation_register.py`` — the websocket/channel mappings are
  updated across several dicts non-atomically (a concurrent unregister can
  leave half-applied state).
* ``runtime/count_call_register.py`` — ``register``/``unregister`` mutate
  the counter/trigger maps unlocked (an ``unregister`` interleaving with
  the locked ``increase`` can crash it with ``KeyError``).
* ``runtime/timer_call_register.py`` — ``session_id_to_timers`` mutated
  unlocked (``unregister`` vs ``reset_timer`` race → ``KeyError``).
* ``runtime/_callback_executor.py`` — ``_ensure_running`` can double-spawn
  the background loop thread under concurrent first use.

Test method: races are made DETERMINISTIC with instrumented fakes
(``SlowDict`` sleeps inside ``__contains__``/``__getitem__`` to widen
check-then-act windows; ``SleepyWs`` sleeps inside its ``.id`` property to
park a thread between the three dict writes of ``register_websocket``) plus
barriers, then invariants are asserted (single creation, snapshot not live
dict, no lost keys, cross-dict consistency, single spawn).
"""

import threading
import time
from typing import Any

import pytest
from loguru import logger

logger.remove()

from runtime._callback_executor import CallbackExecutor  # noqa: E402
import runtime._callback_executor as cb_executor_mod  # noqa: E402
from runtime.core import Register  # noqa: E402
from runtime.count_call_register import CountCallRegister  # noqa: E402
from runtime.relation_register import RelationManager  # noqa: E402
from runtime.state_register import StateRegisterMeM  # noqa: E402
from runtime.timer_call_register import Timer, TimerCallRegister  # noqa: E402

pytestmark = [pytest.mark.unit, pytest.mark.timeout(60)]


# ---------------------------------------------------------------------------
# Instrumented fakes
# ---------------------------------------------------------------------------


class SlowDict(dict):
    """dict that sleeps in __contains__/__getitem__ to widen race windows."""

    def __init__(self, *args, delay: float = 0.05, **kwargs):
        super().__init__(*args, **kwargs)
        self._delay = delay
        self.setitem_count = 0

    def __contains__(self, key) -> bool:
        time.sleep(self._delay)
        return super().__contains__(key)

    def __getitem__(self, key):
        time.sleep(self._delay)
        return super().__getitem__(key)

    def __setitem__(self, key, value) -> None:
        self.setitem_count += 1
        super().__setitem__(key, value)


class RampSlowDict(dict):
    """dict whose FIRST contains call sleeps long, later ones short.

    Two threads racing on a check-then-act ``key not in d`` both observe the
    key missing regardless of arrival order (the late thread finishes its
    short check while the early thread is still asleep), which makes the
    lost-update DETERMINISTIC instead of scheduler-dependent.
    """

    def __init__(self, *args, first_delay: float = 0.1, rest_delay: float = 0.005, **kwargs):
        super().__init__(*args, **kwargs)
        self._first_delay = first_delay
        self._rest_delay = rest_delay
        self._contains_calls = 0
        self.setitem_count = 0

    def __contains__(self, key) -> bool:
        self._contains_calls += 1
        time.sleep(self._first_delay if self._contains_calls == 1 else self._rest_delay)
        return super().__contains__(key)

    def __setitem__(self, key, value) -> None:
        self.setitem_count += 1
        super().__setitem__(key, value)


class RaceWriteDict(dict):
    """Fast __contains__; the FIRST __setitem__ sleeps BEFORE storing.

    This parks the winning thread inside its own ``d[key] = value`` write,
    so every other thread's ``key not in d`` check still sees the dict empty
    and proceeds to create/write — the lost update is deterministic instead
    of scheduler-dependent. Counts all setitem calls.
    """

    def __init__(self, *args, first_write_delay: float = 0.1, **kwargs):
        super().__init__(*args, **kwargs)
        self._first_write_delay = first_write_delay
        self.setitem_count = 0

    def __setitem__(self, key, value) -> None:
        self.setitem_count += 1
        if self.setitem_count == 1:
            time.sleep(self._first_write_delay)
        super().__setitem__(key, value)


class SleepyWs:
    """Fake websocket whose .id sleeps from the Nth access onwards.

    register_websocket reads ``websocket.id`` once per dict write; sleeping
    from access 2 parks the writer between dict writes, opening the window
    a concurrent unregister needs to observe torn state.
    """

    def __init__(self, ws_id: str, sleep_from_access: int = 2, delay: float = 0.05):
        self._id = ws_id
        self._accesses = 0
        self._sleep_from = sleep_from_access
        self._delay = delay

    @property
    def id(self) -> str:
        self._accesses += 1
        if self._accesses >= self._sleep_from:
            time.sleep(self._delay)
        return self._id


class StubExecutor:
    """Stands in for CallbackExecutor so tests never spawn real loop threads."""

    def __init__(self, *args, **kwargs):
        self.cancelled: list[str] = []
        self.created: list[str] = []

    def cancel_task(self, name: str) -> None:
        self.cancelled.append(name)

    def create_task(self, coro, name: str = None, timeout: float = 3600) -> threading.Event:
        coro.close()  # avoid never-awaited warnings
        self.created.append(name)
        return threading.Event()

    def run_coroutine(self, coro, timeout: float = 3600) -> None:
        coro.close()


def _fresh(cls) -> Any:
    """Build an isolated instance of a Register subclass (bypasses singleton)."""
    inst = object.__new__(cls)
    inst._initialized = False
    inst.__init__()
    return inst


def _run_threads(workers: list) -> None:
    threads = [threading.Thread(target=w) for w in workers]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
        assert not t.is_alive(), "worker thread hung"


# ---------------------------------------------------------------------------
# runtime/core.py — Register.__new__ + clear_all_register_sessions
# ---------------------------------------------------------------------------


class TestRegisterSingleton:
    def test_concurrent_first_instantiation_creates_one_instance(self):
        """Two threads racing on a cold singleton must create ONE instance.

        Old code: both threads sleep inside ``cls not in cls._instances``
        (SlowDict.__contains__), both see the class missing, both build an
        instance → two ``_instances[cls] =`` writes, one instance leaked.
        """
        created: list = []
        probe = type(
            "ProbeRegister",
            (Register,),
            {"clear_session": lambda self, session_id: created.append(session_id)},
        )
        slow = RaceWriteDict()
        probe._instances = slow  # shadow: keep the global registry untouched

        barrier = threading.Barrier(2)
        results: list = []

        def worker():
            barrier.wait()
            results.append(probe())

        _run_threads([worker, worker])

        assert len(results) == 2
        assert len({id(r) for r in results}) == 1, "both threads must share one instance"
        assert slow.setitem_count == 1, (
            f"expected exactly one instance creation, saw {slow.setitem_count} "
            "(a second write means the loser was created and leaked)"
        )

    def test_clear_all_register_sessions_under_concurrency(self):
        """Concurrent clear_all runs must not raise and must reach instances."""
        cleared: list[str] = []

        def make(name):
            return type(
                name,
                (Register,),
                {"clear_session": lambda self, session_id: cleared.append(session_id)},
            )

        probe_a = make("ClearProbeA")
        probe_b = make("ClearProbeB")
        probe_a()  # register instances in the global registry
        probe_b()
        try:
            barrier = threading.Barrier(8)
            errors: list[Exception] = []

            def worker():
                barrier.wait()
                try:
                    Register.clear_all_register_sessions("sess-clear")
                except Exception as e:  # pragma: no cover - red path
                    errors.append(e)

            _run_threads([worker] * 8)

            assert not errors, f"clear_all raised under concurrency: {errors}"
            assert cleared.count("sess-clear") >= 2, "both registers must be cleared"
        finally:
            Register._instances.pop(probe_a, None)
            Register._instances.pop(probe_b, None)


# ---------------------------------------------------------------------------
# runtime/state_register.py — StateRegisterMeM
# ---------------------------------------------------------------------------


class TestStateRegisterMeM:
    def test_get_all_states_returns_snapshot_not_live_dict(self):
        reg = _fresh(StateRegisterMeM)
        reg.set_state("s1", "k1", "v1")

        snapshot = reg.get_all_states("s1")
        snapshot["injected"] = True  # caller mutates the returned mapping

        assert reg.get_state("s1", "injected") is None, (
            "get_all_states returned the LIVE dict — caller mutation reached shared state"
        )
        assert reg.get_all_states("s1") == {"k1": "v1"}

    def test_set_state_concurrent_first_touch_no_lost_keys(self, monkeypatch):
        """N threads first-touching the SAME new session must not lose keys.

        Old code: thread 1's ``self._states[sid] = {}`` write is parked by
        RaceWriteDict before it stores; every other thread's
        ``session_id not in self._states`` still sees the session missing and
        assigns its own fresh ``{}`` — when the parked write lands it wipes
        every key written in between.
        """
        reg = _fresh(StateRegisterMeM)
        n = 12
        race = RaceWriteDict()
        monkeypatch.setattr(reg, "_states", race)

        barrier = threading.Barrier(n)

        def worker(i: int):
            barrier.wait()
            reg.set_state("sess", f"k{i}", i)

        _run_threads([lambda i=i: worker(i) for i in range(n)])

        expected = {f"k{i}": i for i in range(n)}
        assert reg.get_all_states("sess") == expected, (
            f"lost updates under first-touch race: got {reg.get_all_states('sess')}"
        )


# ---------------------------------------------------------------------------
# runtime/relation_register.py — multi-dict websocket/channel mappings
# ---------------------------------------------------------------------------


def _relation_consistent(reg) -> bool:
    ws_ids = set(reg.websocket_id_to_ws)
    by_ws = set(reg.websocket_id_to_session_id)
    by_session = set(reg.session_id_to_websocket_id)
    return (
        ws_ids == by_ws
        and by_session == ws_ids
        and set(reg.session_id_to_websocket_id.values()) == ws_ids
    )


class TestRelationManager:
    def test_register_websocket_torn_write_detected(self):
        """register_websocket's three dict writes must be atomic.

        The sleeping .id parks the registering thread between writes; a
        concurrent unregister then removes the single already-written entry,
        and the woken thread writes the remaining two — leaving dict2/dict3
        populated while dict1 is empty.
        """
        reg = _fresh(RelationManager)
        sleepy = SleepyWs("ws-1", sleep_from_access=2)
        done = threading.Event()

        def registrant():
            reg.register_websocket("sess-1", sleepy)
            done.set()

        def unregistrant():
            # Wait until the registrant is parked inside its second .id read.
            deadline = time.time() + 5
            while time.time() < deadline and len(reg.websocket_id_to_session_id) == 0:
                time.sleep(0.005)
            reg.unregister_websocket_by_websocket_id("ws-1")

        t1 = threading.Thread(target=registrant)
        t2 = threading.Thread(target=unregistrant)
        t1.start()
        t2.start()
        t1.join(timeout=30)
        t2.join(timeout=30)
        assert done.is_set()

        assert _relation_consistent(reg), (
            f"torn multi-dict state: ws={dict(reg.websocket_id_to_ws)}, "
            f"by_ws={dict(reg.websocket_id_to_session_id)}, "
            f"by_session={dict(reg.session_id_to_websocket_id)}"
        )

    def test_channel_mappings_consistent_under_register_unregister_stress(self):
        reg = _fresh(RelationManager)
        errors: list[Exception] = []

        def churn(i: int):
            try:
                for round_ in range(40):
                    sid = f"s-{i}-{round_}"
                    reg.register_channel_chat(sid, "qq", f"c-{i}-{round_}")
                    reg.unregister_channel_chat_by_session_id(sid)
            except Exception as e:  # pragma: no cover - red path
                errors.append(e)

        _run_threads([lambda i=i: churn(i) for i in range(6)])

        assert not errors
        assert reg.session_id_to_channel_chat_id == {}
        assert reg.channel_chat_id_to_session_id == {}


# ---------------------------------------------------------------------------
# runtime/count_call_register.py — unlocked register/unregister vs increase
# ---------------------------------------------------------------------------


class TestCountCallRegister:
    def test_unregister_during_locked_increase_does_not_crash(self, monkeypatch):
        """unregister must not interleave with increase's locked read.

        increase() fetches the Trigger via ``[name]`` while holding _lock;
        the SlowDict sleep parks it there. Old unlocked unregister deletes
        the trigger out from under it → KeyError. With unregister taking the
        same lock, the delete can only happen before or after the increase.
        """
        reg = _fresh(CountCallRegister)
        reg.register("sess", "counter", callback=lambda: None, threshold=1_000_000)

        slow_triggers = SlowDict({"counter": reg.session_id_to_trigger["sess"]["counter"]})
        monkeypatch.setattr(reg, "session_id_to_trigger", {"sess": slow_triggers})

        errors: list[Exception] = []

        def increaser():
            for _ in range(50):
                try:
                    reg.increase("sess", "counter")
                except Exception as e:  # pragma: no cover - red path
                    errors.append(e)

        def unreg():
            time.sleep(0.02)
            try:
                reg.unregister("sess", "counter")
            except Exception as e:  # pragma: no cover - red path
                errors.append(e)

        t1 = threading.Thread(target=increaser)
        t2 = threading.Thread(target=unreg)
        t1.start()
        t2.start()
        t1.join(timeout=30)
        t2.join(timeout=30)

        assert not errors, f"increase/unregister race raised: {errors}"

    def test_register_unregister_concurrent_no_ghost_entries(self):
        reg = _fresh(CountCallRegister)
        errors: list[Exception] = []

        def churn(i: int):
            try:
                for round_ in range(40):
                    name = f"c-{i}-{round_}"
                    assert reg.register("sess", name, callback=lambda: None) is True
                    assert reg.increase("sess", name) is True
                    assert reg.unregister("sess", name) is True
            except Exception as e:  # pragma: no cover - red path
                errors.append(e)

        _run_threads([lambda i=i: churn(i) for i in range(6)])

        assert not errors
        assert reg.session_id_to_counter == {"sess": {}}, reg.session_id_to_counter
        assert reg.session_id_to_trigger == {"sess": {}}, reg.session_id_to_trigger


# ---------------------------------------------------------------------------
# runtime/timer_call_register.py — unlocked unregister vs reset_timer
# ---------------------------------------------------------------------------


class TestTimerCallRegister:
    def test_unregister_vs_reset_timer_no_keyerror(self, monkeypatch):
        """Both paths read-then-delete the same timer entry; without a lock
        the loser raises KeyError."""
        reg = _fresh(TimerCallRegister)
        reg._executor = StubExecutor()
        timer = Timer(minutes=1, callback=lambda: None)
        timer.task_name = "task-a"
        reg.session_id_to_timers["sess"] = {"a": timer}

        slow = SlowDict({"a": timer})
        monkeypatch.setattr(reg, "session_id_to_timers", {"sess": slow})

        barrier = threading.Barrier(2)
        errors: list[Exception] = []

        def resetter():
            barrier.wait()
            try:
                reg.reset_timer("sess", "a")
            except Exception as e:  # pragma: no cover - red path
                errors.append(e)

        def unreg():
            barrier.wait()
            try:
                reg.unregister("sess", "a")
            except Exception as e:  # pragma: no cover - red path
                errors.append(e)

        _run_threads([resetter, unreg])

        assert not errors, f"reset/unregister race raised: {errors}"
        assert reg._executor.cancelled == ["task-a"]

    def test_clear_session_cancels_every_timer_once(self):
        reg = _fresh(TimerCallRegister)
        reg._executor = StubExecutor()
        timers = {}
        for name in ("a", "b", "c"):
            t = Timer(minutes=1, callback=lambda: None)
            t.task_name = f"task-{name}"
            timers[name] = t
        reg.session_id_to_timers["sess"] = timers

        errors: list[Exception] = []

        def clearer():
            try:
                reg.clear_session("sess")
            except Exception as e:  # pragma: no cover - red path
                errors.append(e)

        _run_threads([clearer, clearer, clearer])

        assert not errors
        assert "sess" not in reg.session_id_to_timers
        assert sorted(reg._executor.cancelled) == ["task-a", "task-b", "task-c"]


# ---------------------------------------------------------------------------
# runtime/_callback_executor.py — double-spawn of the background loop thread
# ---------------------------------------------------------------------------


class TestCallbackExecutor:
    def test_concurrent_ensure_running_spawns_exactly_one_thread(self, monkeypatch):
        executor = CallbackExecutor(name="test-callback-executor")
        spawned: list[threading.Thread] = []
        real_thread = threading.Thread

        class CountingThread(real_thread):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                spawned.append(self)

                def slow_start(t=self):
                    time.sleep(0.05)  # widen the double-spawn window
                    real_thread.start(t)

                self.start = slow_start

        monkeypatch.setattr(cb_executor_mod.threading, "Thread", CountingThread)

        barrier = threading.Barrier(2)
        loops: list = []

        def worker():
            barrier.wait()
            loops.append(executor.loop)

        _run_threads([worker, worker])

        # The patch also wraps this test's own worker threads — count only
        # the executor's loop thread.
        executor_threads = [t for t in spawned if t.name == "test-callback-executor"]
        assert len(executor_threads) == 1, (
            f"expected a single background loop thread, spawned {len(executor_threads)}"
        )
        assert len(loops) == 2
        assert all(loop is not None for loop in loops)
        assert len({id(loop) for loop in loops}) == 1, "threads must share one loop"
