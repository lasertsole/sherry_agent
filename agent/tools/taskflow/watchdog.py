"""Task-level watchdog + deadlock detection for TaskFlow (WorkMode stage 2).

Sherry already has CALL-level reliability (InvalidToolCallRepair, LLMRetry,
heartbeat_staleness) and a SLOW liveness sweep that FAILS flows idle for hours
(``sweeper._liveness_sweep``). This module adds the missing TASK-level layer:

* a fast **stale heartbeat** notice (10 min by default, well below the hours-long
  liveness cutoff) — a flow that stopped sending updates is flagged early;
* three **deadlock** signals, all derived from state already in ``state_json``:
  1. a step re-dispatched ``deadlock_tool_fail_count`` times, still non-terminal
     and with no judge feedback → suspected same-args tool-failure loop;
  2. a step re-dispatched ``deadlock_judge_fail_count`` times WITH judge feedback
     → suspected judge-retry loop;
  3. a flow whose ``updated_at`` has not moved for ``deadlock_no_progress_s`` →
     no-progress stall.

Fail-open contract
------------------
Detection NEVER mutates a flow. ``scan_tasks`` only emits ``WatchdogAlert``
records; ``notify_alerts`` turns them into best-effort ``system.notice`` cards
("系统检测到任务可能陷入循环" / stale heartbeat). It is the agent/user who
decides to cancel or continue. Every IO error degrades to "no alerts" for that
item — a detection hiccup can never break the sweeper that hosts it.

The detector is a pure-ish function (injectable ``now``/``flows``) so it is
unit-testable without a real DB; production passes flows read from the store.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

from loguru import logger

from agent.tools.taskflow.config import TERMINAL_STATUSES
from config.features import TASKFLOW_INFRA

__all__ = ["WatchdogAlert", "notify_alerts", "scan_tasks"]


#: Alert kinds (stable strings for notices and tests).
STALE_HEARTBEAT = "stale_heartbeat"
TOOL_FAILURE_LOOP = "tool_failure_loop"
JUDGE_RETRY_LOOP = "judge_retry_loop"
NO_PROGRESS = "no_progress"


@dataclass(frozen=True)
class WatchdogAlert:
    """One detection. Immutable so it can be logged/serialised freely."""

    flow_id: str
    session_id: str
    kind: str
    detail: str

    def notice_title(self) -> str:
        return {
            STALE_HEARTBEAT: "任务心跳停滞",
            TOOL_FAILURE_LOOP: "疑似工具重试循环",
            JUDGE_RETRY_LOOP: "疑似判定重试循环",
            NO_PROGRESS: "任务长时间无进展",
        }.get(self.kind, "任务 watchdog 提醒")


def _status_of(flow: dict) -> str:
    return str(flow.get("status") or "")


def _updated_at(flow: dict) -> float:
    try:
        return float(flow.get("updated_at") or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _steps(flow: dict) -> list[dict]:
    state = flow.get("state")
    if not isinstance(state, dict):
        return []
    steps = state.get("steps")
    if not isinstance(steps, list):
        return []
    return [step for step in steps if isinstance(step, dict)]


def _retry_count(step: dict) -> int:
    try:
        return max(0, int(step.get("retry_count") or 0))
    except (TypeError, ValueError):
        return 0


def scan_tasks(
    flows: list[dict],
    *,
    now: float | None = None,
    config: dict[str, Any] | None = None,
) -> list[WatchdogAlert]:
    """Inspect non-terminal *flows* and return fail-open watchdog alerts.

    ``flows`` is normally ``get_non_terminal_flows()`` output; passing an
    explicit list keeps this function a pure detector. ``config`` allows tests
    to override ``TASKFLOW_INFRA`` without mutating global config.
    """
    cfg = config if config is not None else TASKFLOW_INFRA
    current = time.time() if now is None else float(now)
    hb = float(cfg["heartbeat_timeout_s"])
    tool_n = int(cfg["deadlock_tool_fail_count"])
    judge_n = int(cfg["deadlock_judge_fail_count"])
    no_progress = float(cfg["deadlock_no_progress_s"])

    alerts: list[WatchdogAlert] = []
    for flow in flows:
        if not isinstance(flow, dict):
            continue
        status = _status_of(flow)
        if status in TERMINAL_STATUSES:
            continue
        flow_id = str(flow.get("flow_id") or "")
        session_id = str(flow.get("session_id") or "")
        if not flow_id:
            continue
        age = current - _updated_at(flow)

        # Stale heartbeat — early notice, never fails the flow.
        if hb > 0 and age > hb:
            alerts.append(
                WatchdogAlert(
                    flow_id,
                    session_id,
                    STALE_HEARTBEAT,
                    f"flow {flow_id} has not been updated for {int(age)}s (threshold {int(hb)}s)",
                )
            )

        # No-progress stall (only meaningful when longer than heartbeat).
        if no_progress > 0 and age > no_progress:
            alerts.append(
                WatchdogAlert(
                    flow_id,
                    session_id,
                    NO_PROGRESS,
                    f"flow {flow_id} has had no step-status change for {int(age)}s "
                    f"(threshold {int(no_progress)}s)",
                )
            )

        # Per-step retry loops.
        for step in _steps(flow):
            step_status = str(step.get("status") or "")
            if step_status in TERMINAL_STATUSES:
                continue
            retries = _retry_count(step)
            step_id = str(step.get("step_id") or "?")
            has_judge = bool(str(step.get("judge_feedback") or "").strip())
            if judge_n > 0 and has_judge and retries >= judge_n:
                alerts.append(
                    WatchdogAlert(
                        flow_id,
                        session_id,
                        JUDGE_RETRY_LOOP,
                        f"step {step_id} retried {retries}x with judge feedback",
                    )
                )
            elif tool_n > 0 and not has_judge and retries >= tool_n:
                alerts.append(
                    WatchdogAlert(
                        flow_id,
                        session_id,
                        TOOL_FAILURE_LOOP,
                        f"step {step_id} re-dispatched {retries}x without success",
                    )
                )
    return alerts


async def notify_alerts(alerts: list[WatchdogAlert]) -> int:
    """Broadcast each alert as a best-effort system notice; return sent count.

    Uses ``push_system_notice`` (frozen, never raises on a dead socket). Errors
    are swallowed per-alert so one bad session cannot suppress the rest.
    """
    if not alerts:
        return 0
    from runtime.session.system_notice import push_system_notice

    sent = 0
    for alert in alerts:
        if not alert.session_id:
            continue
        try:
            await push_system_notice(
                alert.session_id,
                title=alert.notice_title(),
                content=f"{alert.detail} 系统检测到任务可能陷入循环；请决定取消或继续。",
                level="warning",
                source="taskflow:watchdog",
            )
            sent += 1
        except Exception:  # noqa: BLE001 - best-effort notification
            logger.debug("watchdog notice failed for flow {}", alert.flow_id, exc_info=True)
    return sent


async def sweep_and_notify(
    *,
    now: float | None = None,
    config: dict[str, Any] | None = None,
) -> dict[str, int]:
    """Production entry: read non-terminal flows, scan, notify. Fail-open."""
    from agent.tools.taskflow.registry import store_sqlite as taskflow_store

    try:
        flows = await taskflow_store.get_non_terminal_flows()
    except Exception as exc:  # noqa: BLE001 - fail-open
        logger.warning("TaskFlow watchdog sweep could not read flows: {}", exc)
        return {"alerts": 0, "notified": 0}
    alerts = scan_tasks(flows, now=now, config=config)
    notified = await notify_alerts(alerts)
    if alerts:
        logger.info("TaskFlow watchdog: {} alert(s), {} notified", len(alerts), notified)
    return {"alerts": len(alerts), "notified": notified}
