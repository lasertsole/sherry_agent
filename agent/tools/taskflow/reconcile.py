"""Conservative reconciliation of TaskFlow rows whose owner session is gone.

TaskFlow has no database-level owner lease. A session can disappear (purge,
manual deletion, a crash mid-purge) while its flow rows survive; without a scan
those rows stay non-terminal forever and keep consuming the session's active-flow
budget. This module is the safety net, and it is deliberately conservative:

* a flow is a candidate only when its owner session is **proven missing** by the
  ``runtime.hooks.SESSION_EXISTS`` hook (the server's messages store owns that
  fact) — a missing hook, or a raising callback, means UNKNOWN and the scan does
  nothing;
* a flow with ANY live child run is never touched (the child may still finish
  and resume it);
* the owner has to stay missing for ``TASKFLOW_INFRA["orphan_grace_seconds"]``
  before the flow is finalized, so a transient read gap cannot cancel a live
  flow;
* finalizing means marking the flow CANCELLED and cancelling its unresolved
  steps. It NEVER kills a child session and never deletes a flow row — the
  user's "no false kills" rule.
"""

from __future__ import annotations

import time
from collections.abc import Callable

from loguru import logger

from config.features import TASKFLOW_INFRA
from runtime import hooks

from . import config as flow_config
from .registry import store_sqlite

__all__ = ["child_session_keys", "reconcile_orphaned_flows", "reset_missing_state"]

#: flow_id -> first monotonic-ish wall-clock time the owner was seen missing.
_MISSING_SINCE: dict[str, float] = {}


def reset_missing_state() -> None:
    """Clear the in-process grace map (tests / a fresh process)."""
    _MISSING_SINCE.clear()


def child_session_keys(flow: dict) -> list[str]:
    """Every child session bound to this flow (row-level + per-step)."""
    keys: list[str] = []
    row_key = str(flow.get("child_session_key") or "").strip()
    if row_key:
        keys.append(row_key)
    steps = (flow.get("state") or {}).get("steps") or []
    for step in steps:
        if not isinstance(step, dict):
            continue
        key = str(step.get("child_session_key") or "").strip()
        if key and key not in keys:
            keys.append(key)
    return keys


def _default_session_exists() -> Callable[[str], bool] | None:
    checker = hooks.resolve(hooks.SESSION_EXISTS)
    return checker if callable(checker) else None


def _default_child_is_live() -> Callable[[str], bool] | None:
    """Resolve the subagent registry liveness seam; None when unavailable.

    ``None`` means "cannot prove a child is dead", so the caller must skip the
    scan entirely (fail-open) rather than finalize a flow whose child may run.
    """
    try:
        from agent.tools.subagent.registry.helpers import is_live_unended_run
        from agent.tools.subagent.registry.queries import get_run_by_child_session_key
    except Exception:  # noqa: BLE001 - optional subsystem / import cycle
        return None

    def _live(key: str) -> bool:
        run = get_run_by_child_session_key(key)
        return run is not None and bool(is_live_unended_run(run))

    return _live


def _cancel_steps(state: dict, reason: str) -> None:
    """Mark unresolved steps CANCELLED; done steps keep their outcome."""
    steps = state.get("steps") or []
    for step in steps:
        if not isinstance(step, dict):
            continue
        status = step.get("status")
        if status in (
            flow_config.StepStatus.DONE.value,
            flow_config.StepStatus.FAILED.value,
            flow_config.StepStatus.SKIPPED.value,
            flow_config.StepStatus.CANCELLED.value,
        ):
            continue
        step["status"] = str(flow_config.StepStatus.CANCELLED)
        step["cancel_reason"] = reason


async def reconcile_orphaned_flows(
    *,
    session_exists: Callable[[str], bool] | None = None,
    child_is_live: Callable[[str], bool] | None = None,
    now: float | None = None,
    grace_seconds: float | None = None,
) -> list[str]:
    """Cancel non-terminal flows whose owner session is gone and has no live child.

    Injectable seams exist for tests (the production path resolves the server
    session hook and the subagent registry). Returns the finalized flow ids.
    """
    checker = session_exists if session_exists is not None else _default_session_exists()
    live_child = child_is_live if child_is_live is not None else _default_child_is_live()
    if checker is None or live_child is None:
        return []

    current = time.time() if now is None else float(now)
    grace = (
        float(TASKFLOW_INFRA["orphan_grace_seconds"]) if grace_seconds is None else grace_seconds
    )
    try:
        flows = await store_sqlite.get_non_terminal_flows()
    except Exception as exc:  # noqa: BLE001 - a store hiccup must not affect the sweeper
        logger.warning("TaskFlow orphan reconciliation could not read the store: {}", exc)
        return []

    finalized: list[str] = []
    seen: set[str] = set()
    for flow in flows:
        flow_id = str(flow.get("flow_id") or "").strip()
        owner = str(flow.get("session_id") or "").strip()
        if not flow_id:
            continue
        seen.add(flow_id)
        # Legacy pre-isolation rows have no owner to prove; leave them to the
        # deadline/WAITING sweeps instead of guessing.
        if not owner:
            continue

        keys = child_session_keys(flow)
        try:
            if any(live_child(key) for key in keys):
                _MISSING_SINCE.pop(flow_id, None)
                continue
            owner_alive = bool(checker(owner))
        except Exception:  # noqa: BLE001 - an unreadable fact is UNKNOWN, never "dead"
            _MISSING_SINCE.pop(flow_id, None)
            continue
        if owner_alive:
            _MISSING_SINCE.pop(flow_id, None)
            continue

        first_missing = _MISSING_SINCE.setdefault(flow_id, current)
        if current - first_missing < grace:
            continue

        # Re-read before writing: a child may have started, the owner may have
        # come back, or another writer may have advanced the flow during grace.
        try:
            fresh = await store_sqlite.get_flow(flow_id, owner)
        except Exception as exc:  # noqa: BLE001
            logger.debug("TaskFlow orphan re-read failed for {}: {}", flow_id, exc)
            continue
        if fresh is None or fresh.get("status") in flow_config.TERMINAL_STATUSES:
            _MISSING_SINCE.pop(flow_id, None)
            continue
        try:
            fresh_keys = child_session_keys(fresh)
            if any(live_child(key) for key in fresh_keys) or bool(checker(owner)):
                _MISSING_SINCE.pop(flow_id, None)
                continue
        except Exception:  # noqa: BLE001
            _MISSING_SINCE.pop(flow_id, None)
            continue

        reason = "orphaned: owner session no longer exists (no live child)"
        state = dict(fresh.get("state") or {})
        _cancel_steps(state, reason)
        state["cancel_reason"] = reason
        try:
            await store_sqlite.update_flow(
                flow_id,
                fresh["expected_revision"],
                session_id=owner,
                state=state,
                wait=None,
                status=flow_config.TaskFlowStatus.CANCELLED.value,
                terminal_reason="orphaned",
            )
        except (store_sqlite.FlowConflictError, store_sqlite.FlowNotFoundError) as exc:
            # A concurrent writer won or the row vanished: leave it for the next
            # scan; the write is idempotent.
            logger.debug("TaskFlow orphan finalize skipped for {}: {}", flow_id, exc)
            continue
        except Exception as exc:  # noqa: BLE001
            logger.warning("TaskFlow orphan finalize failed for {}: {}", flow_id, exc)
            continue
        _MISSING_SINCE.pop(flow_id, None)
        finalized.append(flow_id)
        logger.warning(
            "TaskFlow '{}' orphaned (owner session {} missing, no live child); marked cancelled",
            flow_id,
            owner,
        )

    # Drop grace entries for flows no longer active (terminal or deleted).
    for flow_id in list(_MISSING_SINCE):
        if flow_id not in seen:
            _MISSING_SINCE.pop(flow_id, None)
    return finalized
