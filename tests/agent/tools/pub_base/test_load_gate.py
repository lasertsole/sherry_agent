"""The load gate: thresholds, fail-open, and the shared zero-blocking snapshot.

The gate sits in FRONT of the SUBAGENT lane as the environment-aware half of
backpressure (the lane counts "how many"; this asks "can the machine still
carry one more"). Only the 95% band stops anything — the 90% band is advisory
because the lane cap already throttles there.
"""

from __future__ import annotations

import asyncio
import time

import pytest

import agent.tools.pub_base.load_gate as load_gate
from agent.tools.pub_base.load_gate import check_load_gate, get_load_gate, reset_load_gate
from config.features import LANE_SYSTEM

pytestmark = [pytest.mark.unit, pytest.mark.timeout(60)]


@pytest.fixture(autouse=True)
def _gate_on(monkeypatch: pytest.MonkeyPatch):
    """Every test starts from the shipped thresholds with the gate enabled."""
    monkeypatch.setitem(LANE_SYSTEM, "load_gate_enabled", True)
    monkeypatch.setitem(LANE_SYSTEM, "load_gate_cpu_warn", 90)
    monkeypatch.setitem(LANE_SYSTEM, "load_gate_cpu_stop", 95)
    monkeypatch.setitem(LANE_SYSTEM, "load_gate_ram_warn", 90)
    monkeypatch.setitem(LANE_SYSTEM, "load_gate_ram_stop", 95)
    monkeypatch.setitem(LANE_SYSTEM, "load_gate_sample_interval_s", 0.0)
    monkeypatch.setitem(LANE_SYSTEM, "load_gate_snapshot_ttl_s", 5.0)
    reset_load_gate()
    yield
    reset_load_gate()


def _fake_psutil(monkeypatch: pytest.MonkeyPatch, *, cpu: float, ram: float) -> None:
    """Pin the sampler's inputs: CPU samples and the memory reading."""
    import types

    fake = types.SimpleNamespace(
        cpu_percent=lambda interval=0.0: cpu,
        virtual_memory=lambda: types.SimpleNamespace(total=100.0, available=100.0 - ram),
    )
    monkeypatch.setitem(__import__("sys").modules, "psutil", fake)


class TestThresholds:
    def test_a_quiet_host_passes(self, monkeypatch: pytest.MonkeyPatch):
        _fake_psutil(monkeypatch, cpu=40.0, ram=50.0)

        verdict = check_load_gate()

        assert verdict.ok is True and verdict.stop is False

    def test_the_advisory_band_does_not_stop(self, monkeypatch: pytest.MonkeyPatch):
        _fake_psutil(monkeypatch, cpu=91.0, ram=50.0)

        verdict = check_load_gate()

        assert verdict.stop is False, "90-95% is advisory: the lane cap throttles here"
        assert verdict.ok is True

    def test_the_hard_band_stops(self, monkeypatch: pytest.MonkeyPatch):
        _fake_psutil(monkeypatch, cpu=96.0, ram=50.0)

        verdict = check_load_gate()

        assert verdict.stop is True and verdict.ok is False

    def test_memory_alone_can_stop(self, monkeypatch: pytest.MonkeyPatch):
        _fake_psutil(monkeypatch, cpu=10.0, ram=97.0)

        assert check_load_gate().stop is True

    def test_the_stop_check_is_not_fake(self, monkeypatch: pytest.MonkeyPatch):
        """Reversal test (DSH step 4): lower the stop line and any load trips it."""
        _fake_psutil(monkeypatch, cpu=1.0, ram=1.0)
        monkeypatch.setitem(LANE_SYSTEM, "load_gate_cpu_stop", 1)

        assert check_load_gate().stop is True


class TestFailOpen:
    def test_a_broken_monitor_allows_dispatch(self, monkeypatch: pytest.MonkeyPatch):
        import types

        def _boom(interval=0.0):
            raise OSError("no /proc")

        fake = types.SimpleNamespace(
            cpu_percent=_boom, virtual_memory=lambda: types.SimpleNamespace(total=1, available=1)
        )
        monkeypatch.setitem(__import__("sys").modules, "psutil", fake)

        verdict = check_load_gate()

        assert verdict.ok is True and verdict.stop is False, "a dead monitor must not stall work"
        assert verdict.cpu == -1.0

    def test_disabled_never_samples(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setitem(LANE_SYSTEM, "load_gate_enabled", False)
        called: list[int] = []

        import types

        fake = types.SimpleNamespace(
            cpu_percent=lambda interval=0.0: called.append(1) or 99.0,
            virtual_memory=lambda: types.SimpleNamespace(total=1, available=0),
        )
        monkeypatch.setitem(__import__("sys").modules, "psutil", fake)

        verdict = check_load_gate()

        assert verdict.stop is False
        assert called == [], "disabled means zero psutil traffic"


class TestSharedSnapshot:
    @pytest.mark.asyncio
    async def test_concurrent_readers_sample_only_once(self, monkeypatch: pytest.MonkeyPatch):
        samples: list[int] = []

        def _slow_sample() -> load_gate.LoadGateVerdict:
            samples.append(1)
            time.sleep(0.05)  # stand in for the 4.5 s of real CPU sampling
            return load_gate.LoadGateVerdict(ok=False, stop=True, cpu=97.0, ram=50.0)

        monkeypatch.setattr(load_gate, "check_load_gate", _slow_sample)

        results = await asyncio.gather(*(get_load_gate() for _ in range(8)))

        assert len(samples) == 1, "one sample per TTL for the whole process"
        assert all(verdict.stop for verdict in results)

    @pytest.mark.asyncio
    async def test_the_ttl_serves_the_cached_verdict(self, monkeypatch: pytest.MonkeyPatch):
        samples: list[int] = []

        def _sample() -> load_gate.LoadGateVerdict:
            samples.append(1)
            return load_gate.LoadGateVerdict(ok=True, stop=False, cpu=10.0, ram=10.0)

        monkeypatch.setattr(load_gate, "check_load_gate", _sample)

        await get_load_gate()
        await get_load_gate()
        await get_load_gate()

        assert len(samples) == 1

    @pytest.mark.asyncio
    async def test_an_expired_snapshot_resamples(self, monkeypatch: pytest.MonkeyPatch):
        samples: list[int] = []
        monkeypatch.setitem(LANE_SYSTEM, "load_gate_snapshot_ttl_s", 0.0)

        def _sample() -> load_gate.LoadGateVerdict:
            samples.append(1)
            return load_gate.LoadGateVerdict(ok=True, stop=False, cpu=10.0, ram=10.0)

        monkeypatch.setattr(load_gate, "check_load_gate", _sample)

        await get_load_gate()
        await get_load_gate()

        assert len(samples) == 2

    @pytest.mark.asyncio
    async def test_sampling_never_runs_on_the_event_loop(self, monkeypatch: pytest.MonkeyPatch):
        """The blocking sampler must be reached through a worker thread."""
        seen: dict[str, str] = {}

        def _sample() -> load_gate.LoadGateVerdict:
            import threading

            seen["thread"] = threading.current_thread().name
            return load_gate.LoadGateVerdict(ok=True, stop=False, cpu=1.0, ram=1.0)

        monkeypatch.setattr(load_gate, "check_load_gate", _sample)

        await get_load_gate()

        assert seen["thread"] != "MainThread"
