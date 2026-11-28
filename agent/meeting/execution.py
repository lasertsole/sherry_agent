"""Meeting conclusion → TaskFlow execution (plan §5.2, 阶段 4).

Turns a :class:`MeetingConclusion` into a real TaskFlow flow and dispatches it
through the existing expert-panel machinery. All TaskFlow calls go through an
injectable :class:`FlowsPort` so the HTTP route and tests never touch the live
spawn path unless explicitly asked.

The flow id is deterministic per meeting (``meeting-{meeting_id}``), making a
second execute request idempotent: an existing flow is returned instead of a
duplicate.
"""

from __future__ import annotations

import uuid
from typing import Any, Protocol

from loguru import logger

from agent.meeting.conclusion import plan_to_steps
from agent.meeting.models import MeetingConclusion

__all__ = ["FlowsPort", "TaskFlowPort", "execute_conclusion", "make_taskflow_port"]


class TaskFlowPort(Protocol):
    """The concrete TaskFlow seam (get_flow + create_flow + dispatch)."""

    async def get_flow(self, flow_id: str, session_id: str) -> Any: ...

    async def create_flow(self, flow_id: str, state: dict, *, session_id: str) -> Any: ...

    async def dispatch(self, flow_id: str, step_ids: list[str], *, session_id: str) -> Any: ...


class FlowsPort(Protocol):
    """Higher-level seam used by :func:`execute_conclusion`."""

    async def execute(
        self, conclusion: MeetingConclusion, *, meeting_id: str, session_id: str
    ) -> dict[str, Any]: ...


class _DefaultTaskFlowPort:
    """Binds to the product TaskFlow store and batch dispatcher."""

    async def get_flow(self, flow_id: str, session_id: str) -> Any:
        from agent.tools.taskflow.registry import store_sqlite

        return await store_sqlite.get_flow(flow_id, session_id)

    async def create_flow(self, flow_id: str, state: dict, *, session_id: str) -> Any:
        from agent.tools.taskflow.registry import store_sqlite

        return await store_sqlite.create_flow(flow_id, state, session_id=session_id)

    async def dispatch(self, flow_id: str, step_ids: list[str], *, session_id: str) -> Any:
        from agent.tools.taskflow.tools.taskflow_dispatch import _dispatch_batch

        return await _dispatch_batch(
            flow_id=flow_id, step_ids=step_ids, session_id=session_id, auto=True
        )


def make_taskflow_port() -> TaskFlowPort:
    """The real TaskFlow port used in production."""
    return _DefaultTaskFlowPort()


async def execute_conclusion(
    conclusion: MeetingConclusion,
    *,
    meeting_id: str,
    session_id: str,
    port: FlowsPort | None = None,
) -> dict[str, Any]:
    """Create and dispatch a TaskFlow from a meeting conclusion.

    ``port`` defaults to a TaskFlow-backed execution. Returns the flow id, the
    step ids and the raw create/dispatch results for the HTTP layer.
    """
    if port is None:
        port = _TaskFlowFlowsPort(taskflow=make_taskflow_port())
    return await port.execute(conclusion, meeting_id=meeting_id, session_id=session_id)


class _TaskFlowFlowsPort:
    """FlowsPort backed by a TaskFlowPort (the production composition)."""

    def __init__(self, taskflow: TaskFlowPort) -> None:
        self._taskflow = taskflow

    async def execute(
        self, conclusion: MeetingConclusion, *, meeting_id: str, session_id: str
    ) -> dict[str, Any]:
        from agent.tools.taskflow.tools._shared import (
            default_state,
            requester_session_key,
        )

        flow_id = f"meeting-{meeting_id}"
        existing = await self._taskflow.get_flow(flow_id, session_id)
        if existing is not None:
            return {
                "flow_id": flow_id,
                "step_ids": [
                    s.get("step_id", "") for s in (existing.get("state") or {}).get("steps", [])
                ],
                "created": existing,
                "dispatched": None,
                "dispatch_error": None,
                "already_exists": True,
            }
        steps = plan_to_steps(conclusion)
        state = default_state(
            conclusion.taskflow_plan.description,
            {"steps": steps},
            creator_session_key=requester_session_key(session_id),
        )
        created = await self._taskflow.create_flow(flow_id, state, session_id=session_id)
        step_ids = [step["step_id"] for step in steps]
        dispatched = None
        if step_ids:
            try:
                dispatched = await self._taskflow.dispatch(flow_id, step_ids, session_id=session_id)
            except Exception as exc:  # flow exists but dispatch failed — surface it
                logger.warning("meeting flow dispatch failed for {}: {}", flow_id, exc)
                return {
                    "flow_id": flow_id,
                    "step_ids": step_ids,
                    "created": created,
                    "dispatched": None,
                    "dispatch_error": str(exc),
                }
        return {
            "flow_id": flow_id,
            "step_ids": step_ids,
            "created": created,
            "dispatched": dispatched,
            "dispatch_error": None,
        }


def new_execution_id() -> str:
    """A fresh id for callers that need one outside the deterministic flow id."""
    return uuid.uuid4().hex
