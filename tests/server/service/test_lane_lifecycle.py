"""Module tests for server/service/lane_lifecycle.py — startup wiring + exit drain."""

import pytest

import runtime.lane.core as lane_core
from runtime.lane.core import LaneManager, LaneType, lane_slot
from server.service import lane_lifecycle as lc

pytestmark = [pytest.mark.unit]


@pytest.fixture(autouse=True)
def _reset_install(monkeypatch: pytest.MonkeyPatch):
    """Re-arm install() and clear the global drain gate around every test."""
    monkeypatch.setattr(lc, "_installed", False)
    lane_core.set_drain_check(None)
    yield
    lane_core.set_drain_check(None)


class TestInstallLaneLifecycle:
    def test_prewarms_the_manager_and_registers_the_drain_gate(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        registered: list = []
        monkeypatch.setattr(lane_core, "_manager", None)
        monkeypatch.setattr(lc.atexit, "register", lambda fn: registered.append(fn))

        lc.install_lane_lifecycle()

        assert lane_core._manager is not None, "the manager must be prewarmed"

        from agent.tools.subagent.registry import is_gateway_draining

        assert lane_core._drain_check is is_gateway_draining
        assert len(registered) == 1

    def test_is_idempotent(self, monkeypatch: pytest.MonkeyPatch):
        registered: list = []
        monkeypatch.setattr(lane_core, "_manager", None)
        monkeypatch.setattr(lc.atexit, "register", lambda fn: registered.append(fn))

        lc.install_lane_lifecycle()
        lc.install_lane_lifecycle()

        assert len(registered) == 1

    @pytest.mark.asyncio
    async def test_installed_gate_refuses_acquire_while_draining(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        monkeypatch.setattr(lane_core, "_manager", LaneManager())
        monkeypatch.setattr(lc.atexit, "register", lambda fn: None)
        lc.install_lane_lifecycle()

        from agent.tools.subagent.registry import set_draining

        set_draining(True)
        try:
            with pytest.raises(RuntimeError, match="draining"):
                async with lane_slot(LaneType.MAIN):
                    pass
        finally:
            set_draining(False)

    def test_exit_drain_is_bounded_and_flips_draining(self, monkeypatch: pytest.MonkeyPatch):
        from agent.tools.subagent.registry import is_gateway_draining, set_draining

        set_draining(False)
        drain_calls: list[float | None] = []
        observed_draining: list[bool] = []

        async def fake_drain(timeout: float | None = None) -> dict[str, bool]:
            observed_draining.append(is_gateway_draining())
            drain_calls.append(timeout)
            return {"main": True, "subagent": True, "nudge": True, "nested": True}

        monkeypatch.setattr(lc, "drain_all_lanes", fake_drain)
        try:
            lc._drain_lanes_at_exit()
        finally:
            set_draining(False)

        assert observed_draining == [True]
        assert drain_calls == [0], "the exit drain must never block process exit"


class TestDrainAllLanes:
    @pytest.mark.asyncio
    async def test_reports_every_lane(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setattr(lane_core, "_manager", LaneManager())

        results = await lc.drain_all_lanes(timeout=0.05)

        assert set(results) == {"main", "subagent", "nudge", "nested"}
        assert all(results.values())
