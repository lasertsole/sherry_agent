"""WorkMode stage 2: task-level watchdog + deadlock detection.

The detector is fail-open and NOTICE-only — it never mutates a flow. These tests
pin: stale heartbeat, no-progress stall, the two retry-loop signals (with/without
judge feedback), terminal/non-flow filtering, threshold=0 disable, and the
best-effort notification path.
"""

from __future__ import annotations

import pytest

from agent.tools.taskflow import watchdog as wd

pytestmark = [pytest.mark.unit]

NOW = 1_000_000.0
CFG = {
    "heartbeat_timeout_s": 600.0,
    "deadlock_tool_fail_count": 3,
    "deadlock_judge_fail_count": 5,
    "deadlock_no_progress_s": 1800.0,
}


def _flow(
    *,
    flow_id="f-1",
    session_id="s-1",
    status="running",
    updated_at=NOW,
    steps=None,
):
    return {
        "flow_id": flow_id,
        "session_id": session_id,
        "status": status,
        "updated_at": updated_at,
        "state": {"steps": steps or []},
    }


def _step(step_id="s1", status="dispatched", retries=0, judge_feedback=""):
    step = {"step_id": step_id, "status": status, "retry_count": retries}
    if judge_feedback:
        step["judge_feedback"] = judge_feedback
    return step


class TestScanTasks:
    def test_healthy_flow_produces_no_alerts(self):
        alerts = wd.scan_tasks([_flow()], now=NOW, config=CFG)
        assert alerts == []

    def test_stale_heartbeat_alert(self):
        flow = _flow(updated_at=NOW - 700)
        alerts = wd.scan_tasks([flow], now=NOW, config=CFG)
        kinds = {a.kind for a in alerts}
        assert wd.STALE_HEARTBEAT in kinds

    def test_no_progress_alert(self):
        flow = _flow(updated_at=NOW - 2000)
        alerts = wd.scan_tasks([flow], now=NOW, config=CFG)
        kinds = {a.kind for a in alerts}
        assert wd.NO_PROGRESS in kinds
        assert wd.STALE_HEARTBEAT in kinds  # also past the shorter heartbeat

    def test_tool_failure_loop(self):
        flow = _flow(steps=[_step(retries=3)])
        alerts = wd.scan_tasks([flow], now=NOW, config=CFG)
        assert any(a.kind == wd.TOOL_FAILURE_LOOP for a in alerts)

    def test_judge_retry_loop(self):
        flow = _flow(steps=[_step(retries=5, judge_feedback="need more work")])
        alerts = wd.scan_tasks([flow], now=NOW, config=CFG)
        kinds = [a.kind for a in alerts]
        assert wd.JUDGE_RETRY_LOOP in kinds
        assert wd.TOOL_FAILURE_LOOP not in kinds  # judge feedback disambiguates

    def test_below_threshold_no_loop(self):
        flow = _flow(steps=[_step(retries=2)])
        assert wd.scan_tasks([flow], now=NOW, config=CFG) == []

    def test_terminal_step_is_ignored(self):
        flow = _flow(steps=[_step(status="done", retries=9)])
        assert wd.scan_tasks([flow], now=NOW, config=CFG) == []

    def test_terminal_flow_is_ignored(self):
        for status in ("done", "failed", "cancelled"):
            flow = _flow(status=status, updated_at=NOW - 5000, steps=[_step(retries=9)])
            assert wd.scan_tasks([flow], now=NOW, config=CFG) == []

    def test_disabled_thresholds(self):
        disabled = dict(
            CFG,
            heartbeat_timeout_s=0,
            deadlock_tool_fail_count=0,
            deadlock_judge_fail_count=0,
            deadlock_no_progress_s=0,
        )
        flow = _flow(updated_at=NOW - 9999, steps=[_step(retries=99)])
        assert wd.scan_tasks([flow], now=NOW, config=disabled) == []

    def test_malformed_input_is_skipped(self):
        bad = [
            None,
            "not a dict",
            {"flow_id": "", "status": "running"},
            # A malformed state is tolerated (no step scan) but a fresh flow
            # still produces no alert — the detector must not crash.
            {"flow_id": "f", "status": "running", "updated_at": NOW, "state": "not a dict"},
        ]
        assert wd.scan_tasks(bad, now=NOW, config=CFG) == []

    def test_alert_carries_identity_and_title(self):
        flow = _flow(flow_id="f-7", session_id="s-9", updated_at=NOW - 700)
        alert = wd.scan_tasks([flow], now=NOW, config=CFG)[0]
        assert alert.flow_id == "f-7"
        assert alert.session_id == "s-9"
        assert alert.notice_title()


@pytest.mark.asyncio
class TestNotifyAlerts:
    async def test_notifies_each_alert_best_effort(self, monkeypatch):
        sent = []

        async def fake_push(session_id, **kwargs):
            sent.append((session_id, kwargs))
            return {}

        import runtime.session.system_notice as sn

        monkeypatch.setattr(sn, "push_system_notice", fake_push)
        alerts = [
            wd.WatchdogAlert("f1", "s1", wd.STALE_HEARTBEAT, "detail1"),
            wd.WatchdogAlert("f2", "", wd.NO_PROGRESS, "detail2"),  # no session -> skipped
        ]
        count = await wd.notify_alerts(alerts)
        assert count == 1
        assert sent[0][0] == "s1"
        assert sent[0][1]["level"] == "warning"

    async def test_empty_is_noop(self):
        assert await wd.notify_alerts([]) == 0

    async def test_push_error_is_swallowed(self, monkeypatch):
        async def boom(session_id, **kwargs):
            raise RuntimeError("socket dead")

        import runtime.session.system_notice as sn

        monkeypatch.setattr(sn, "push_system_notice", boom)
        alerts = [wd.WatchdogAlert("f1", "s1", wd.STALE_HEARTBEAT, "d")]
        assert await wd.notify_alerts(alerts) == 0  # fail-open
