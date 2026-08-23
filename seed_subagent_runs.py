"""Seed the sub-agent registry with synthetic multi-session task trees.

Purpose
-------
The left "后台任务" tab groups depth-1 root tasks by their calling session
(``requester_session_key``). To demonstrate how several sessions' task
graphs cluster, we inject synthetic run records across multiple requester
session keys, then:

  1. write them into the in-memory registry (``memory.set_run``),
  2. persist them to SQLite (``persist_runs_to_disk``),
  3. broadcast ``subagent_spawned`` / ``subagent_ended`` events over the
     WebSocket channel (``_broadcast``) so a connected 后台任务 client
     receives them live regardless of session.

Construction notes
------------------
* ``requester_session_key`` of a depth-1 root must be ``"default"`` for the
  HTTP ``GET /subagents/runs?session_id=default`` BFS to reach it. Roots from
  other fake sessions are still delivered to the UI via WS broadcast.
* Descendants use the parent's ``child_session_key`` as their own
  ``requester_session_key`` (that is how the BFS links a tree together).
* Enums are constructed with explicit enum instances; ``model_dump(mode="json")``
  serialises them to lowercase (``"running"`` / ``"ok"`` / ``"delivered"``),
  matching the backend wire format.

Usage
-----
    .venv\\Scripts\\python seed_subagent_runs.py

Idempotence
-----------
Deterministic run_id suffixes make re-running replace the same records instead
of duplicating them.
"""

from __future__ import annotations

import asyncio
import time

from agent.tools.subagent.registry import memory, persist_runs_to_disk
from agent.tools.subagent.types.registry import (
    CompletionState,
    CompletionDeliveryState,
    DeliveryStatus,
    ExecutionState,
    ExecutionStatus,
    RunOutcome,
    RunOutcomeStatus,
    SubagentRunRecord,
)
from agent.tools.subagent.types.spawn import SpawnMode, ContextMode  # noqa: F401  (referenced below if used)
from agent.tools.subagent.types.capability import SubagentSessionRole, ControlScope

# Reuse the exact broadcast helper the WS handler uses, so frames follow the
# same serialization path and reach live WebSocket clients.
from server.trigger.ws.subagent_ws import _broadcast

_NOW = time.time()


def _child_key(agent_id: str, seed: str) -> str:
    """Build a session key in the canonical ``agent:{id}:subagent:{uuid}`` shape."""
    from uuid import uuid4

    # seed keeps the key stable/replaceable across runs
    return f"agent:{agent_id}:subagent:{seed or uuid4()}"


def _run(
    *,
    run_seed: str,
    task: str,
    requester_session_key: str,
    child_session_key: str,
    depth: int,
    task_name: str | None = None,
    label: str | None = None,
    status: ExecutionStatus = ExecutionStatus.TERMINAL,
    outcome: RunOutcomeStatus = RunOutcomeStatus.OK,
    delivery: DeliveryStatus = DeliveryStatus.DELIVERED,
    started_delta: float = 60.0,
    ended_delta: float = 5.0,
    result_text: str | None = None,
    error: str | None = None,
    ended_reason: str = "completed",
) -> SubagentRunRecord:
    started_at = _NOW - started_delta
    ended_at = _NOW - ended_delta
    return SubagentRunRecord(
        run_id=f"seed-{run_seed}",
        task_run_id=f"seed-{run_seed}",
        child_session_key=child_session_key,
        requester_session_key=requester_session_key,
        task=task,
        task_name=task_name or f"{task[:24].replace(' ', '_')}",
        label=label,
        spawn_mode=SpawnMode.RUN,
        cleanup="delete",
        depth=depth,
        role=SubagentSessionRole.LEAF if depth >= 2 else SubagentSessionRole.ORCHESTRATOR,
        control_scope=ControlScope.NONE if depth >= 2 else ControlScope.CHILDREN,
        ended_reason=ended_reason,
        agent_id="main",
        execution=ExecutionState(
            status=status,
            started_at=started_at,
            ended_at=ended_at,
            outcome=RunOutcome(status=outcome, error=error),
        ),
        completion=CompletionState(
            required=True,
            result_text=result_text or f"Seed result for {task}",
            captured_at=ended_at,
        ),
        delivery=CompletionDeliveryState(
            status=delivery,
            delivered_at=ended_at,
            announced_at=ended_at,
        ),
    )


def build_trees() -> list[SubagentRunRecord]:
    """Build synthetic task trees for a few distinct caller sessions."""
    runs: list[SubagentRunRecord] = []

    # ---- Session: default (the main chat session visible via HTTP) --------
    # Root (depth 1) - spawns two child leaves (depth 2)
    root_a_child = _child_key("main", "seed-def-a-child")
    root_b_child = _child_key("main", "seed-def-b-child")
    root_a = _run(
        run_seed="def-root-a",
        task="Research user travel preferences",
        task_name="travel_research",
        label="travel_research",
        requester_session_key="default",
        child_session_key=root_a_child,
        depth=1,
        status=ExecutionStatus.TERMINAL,
        outcome=RunOutcomeStatus.OK,
        ended_reason="completed",
        started_delta=300.0,
        ended_delta=40.0,
    )
    leaf_a1 = _run(
        run_seed="def-leaf-a1",
        task="Search top beach destinations for March",
        task_name="beach_search",
        label="beach_search",
        requester_session_key=root_a_child,
        child_session_key=_child_key("main", "seed-def-a1-leaf"),
        depth=2,
        status=ExecutionStatus.TERMINAL,
        outcome=RunOutcomeStatus.OK,
        started_delta=280.0,
        ended_delta=60.0,
    )
    leaf_a2 = _run(
        run_seed="def-leaf-a2",
        task="Estimate trip budget for 5 days",
        task_name="budget_estimate",
        label="budget_estimate",
        requester_session_key=root_a_child,
        child_session_key=_child_key("main", "seed-def-a2-leaf"),
        depth=2,
        status=ExecutionStatus.TERMINAL,
        outcome=RunOutcomeStatus.ERROR,
        ended_reason="error",
        delivery=DeliveryStatus.FAILED,
        error="embedding model timed out",
        result_text=None,
        started_delta=260.0,
        ended_delta=80.0,
    )

    root_b = _run(
        run_seed="def-root-b",
        task="Draft a weekend itinerary skeleton",
        task_name="itinerary_draft",
        label="itinerary_draft",
        requester_session_key="default",
        child_session_key=root_b_child,
        depth=1,
        status=ExecutionStatus.TERMINAL,
        outcome=RunOutcomeStatus.OK,
        started_delta=900.0,
        ended_delta=120.0,
    )
    leaf_b1 = _run(
        run_seed="def-leaf-b1",
        task="List must-see historical sites",
        task_name="history_sites",
        label="history_sites",
        requester_session_key=root_b_child,
        child_session_key=_child_key("main", "seed-def-b1-leaf"),
        depth=2,
        status=ExecutionStatus.TERMINAL,
        outcome=RunOutcomeStatus.OK,
        started_delta=870.0,
        ended_delta=150.0,
    )

    runs += [root_a, leaf_a1, leaf_a2, root_b, leaf_b1]

    # ---- Session: session-omega (a second chat session, WS-delivered) -----
    root_c_child = _child_key("main", "seed-om-c-child")
    root_c = _run(
        run_seed="om-root-c",
        task="Summarize the attached PDF key points",
        task_name="pdf_summary",
        label="pdf_summary",
        requester_session_key="session-omega",
        child_session_key=root_c_child,
        depth=1,
        status=ExecutionStatus.RUNNING,
        outcome=RunOutcomeStatus.UNKNOWN,
        ended_reason="running",
        delivery=DeliveryStatus.PENDING,
        started_delta=20.0,
        ended_delta=0.0,
    )
    leaf_c1 = _run(
        run_seed="om-leaf-c1",
        task="Extract tables from section 3",
        task_name="table_extract",
        label="table_extract",
        requester_session_key=root_c_child,
        child_session_key=_child_key("main", "seed-om-c1-leaf"),
        depth=2,
        status=ExecutionStatus.TERMINAL,
        outcome=RunOutcomeStatus.OK,
        started_delta=18.0,
        ended_delta=3.0,
    )
    runs += [root_c, leaf_c1]

    # ---- Session: session-alpha (a third chat session, WS-delivered) ------
    root_d = _run(
        run_seed="al-root-d",
        task="Compare three hosting providers",
        task_name="hosting_compare",
        label="hosting_compare",
        requester_session_key="session-alpha",
        child_session_key=_child_key("main", "seed-al-d-child"),
        depth=1,
        status=ExecutionStatus.TERMINAL,
        outcome=RunOutcomeStatus.OK,
        started_delta=3600.0,
        ended_delta=300.0,
    )
    runs += [root_d]

    return runs


async def main() -> None:
    runs = build_trees()
    # Write + persist.
    for r in runs:
        memory.set_run(r)
    await persist_runs_to_disk()
    print(f"Seeded {len(runs)} runs into registry memory + SQLite")

    # Live-broadcast spawn + terminal events over WS so the 后台任务 tab updates.
    for r in runs:
        # A real lifecycle fires spawned then ended; synthetic data replays both.
        _broadcast("subagent_spawned", r)
        if r.execution.status == ExecutionStatus.TERMINAL:
            _broadcast("subagent_ended", r)
    print("Broadcast subagent_spawned / subagent_ended events over WS")


if __name__ == "__main__":
    asyncio.run(main())
