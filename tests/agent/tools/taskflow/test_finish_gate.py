"""Finish-gate tests: DAG completeness, evidence and the goal gate.

The finish path is gated before the DONE transition:

* Gate A — every step is done or blocked (none ready/dispatched);
* Gate B — no step is blocked;
* Gate C — the flow's AGGREGATED evidence (its steps' child sessions plus the
  creator's) has no failing/stale rows;

The tests exercise both the pass and reject branches, the omitted-params path
and the fail-open contract (an unavailable evidence
collector or judge never blocks a finish).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agent.tools.taskflow import build_taskflow_tools
from agent.tools.taskflow.registry import store_sqlite
from agent.tools.pub_base.evidence_ledger import EvidenceLedger

pytestmark = [pytest.mark.unit]

SESSION = "sess-finish-gate"
FLOW = "flow-finish-gate"


def _finish_tool():
    return {t.name: t for t in build_taskflow_tools()}["taskflow_finish"]


@pytest.fixture()
def ledger(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> EvidenceLedger:
    """Redirect the shared evidence ledger into a per-test file."""
    monkeypatch.setattr(EvidenceLedger, "LEDGER_PATH", str(tmp_path / "evidence.jsonl"))
    return EvidenceLedger


async def _seed_flow(steps: list[dict]) -> None:
    await store_sqlite.create_flow(
        FLOW,
        {
            "description": "finish gate probe",
            "steps": steps,
            "results": [],
            "creator_session_key": SESSION,
        },
        session_id=SESSION,
    )


def _step(step_id: str, status: str, **extra: object) -> dict:
    return {
        "step_id": step_id,
        "task": f"do {step_id}",
        "depends_on": [],
        "status": status,
        **extra,
    }


# ---------------------------------------------------------------------------
# Gate A / Gate B — DAG completeness and blocked steps
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_finish_succeeds_when_all_steps_done(isolated_db: Path, ledger: EvidenceLedger):
    await _seed_flow([_step("s1", "done"), _step("s2", "done")])

    out = await _finish_tool().coroutine(session_id=SESSION, flow_id=FLOW, summary="ok")

    assert "TaskFlow finished" in out and "status=done" in out


@pytest.mark.asyncio
async def test_finish_rejected_when_step_not_done(isolated_db: Path, ledger: EvidenceLedger):
    await _seed_flow([_step("s1", "done"), _step("s2", "ready")])

    out = await _finish_tool().coroutine(session_id=SESSION, flow_id=FLOW)

    assert out.startswith("Error: Cannot finish")
    assert "have not started" in out and "s2" in out
    assert "protocol violation" in out


@pytest.mark.asyncio
async def test_finish_rejected_when_step_dispatched(isolated_db: Path, ledger: EvidenceLedger):
    await _seed_flow([_step("s1", "dispatched")])

    out = await _finish_tool().coroutine(session_id=SESSION, flow_id=FLOW)

    assert out.startswith("Error: Cannot finish")
    assert "not done/blocked" in out


@pytest.mark.asyncio
async def test_finish_rejected_when_step_blocked(isolated_db: Path, ledger: EvidenceLedger):
    await _seed_flow([_step("s1", "blocked", block_reason="acceptance failed")])

    out = await _finish_tool().coroutine(session_id=SESSION, flow_id=FLOW)

    assert out.startswith("Error: Cannot finish")
    assert "blocked" in out and "acceptance failed" in out


# ---------------------------------------------------------------------------
# Gate C — evidence
# ---------------------------------------------------------------------------


CHILD_A = "agent:main:subagent:child-a"
CHILD_B = "agent:main:subagent:child-b"


@pytest.mark.asyncio
async def test_finish_succeeds_when_evidence_passes(isolated_db: Path, ledger: EvidenceLedger):
    await _seed_flow([_step("s1", "done", child_session_key=CHILD_A)])
    ledger.for_session(CHILD_A).append(kind="test", command="pytest -q", status="passed")

    out = await _finish_tool().coroutine(session_id=SESSION, flow_id=FLOW)

    assert "TaskFlow finished" in out


@pytest.mark.asyncio
async def test_finish_rejected_when_evidence_failed(isolated_db: Path, ledger: EvidenceLedger):
    """The regression: evidence rows carry the STEP'S child session key.

    The old test seeded rows under the flow id, a key no producer writes — the
    gate looked up the same nonexistent key and always passed.
    """
    await _seed_flow([_step("s1", "done", child_session_key=CHILD_A)])
    ledger.for_session(CHILD_A).append(kind="test", command="pytest -q", status="failed")

    out = await _finish_tool().coroutine(session_id=SESSION, flow_id=FLOW)

    assert "failing verification evidence" in out


@pytest.mark.asyncio
async def test_finish_rejected_when_evidence_stale(isolated_db: Path, ledger: EvidenceLedger):
    await _seed_flow([_step("s1", "done", child_session_key=CHILD_A)])
    ledger.for_session(CHILD_A).append(kind="test", command="pytest foo.py", status="passed")
    ledger.for_session(CHILD_A).mark_stale_for_path("foo.py")

    out = await _finish_tool().coroutine(session_id=SESSION, flow_id=FLOW)

    assert "stale evidence" in out


@pytest.mark.asyncio
async def test_finish_aggregates_every_steps_child_session(
    isolated_db: Path, ledger: EvidenceLedger
):
    """Two dispatched steps, one failing verification → the flow is rejected."""
    await _seed_flow(
        [
            _step("s1", "done", child_session_key=CHILD_A),
            _step("s2", "done", child_session_key=CHILD_B),
        ]
    )
    ledger.for_session(CHILD_A).append(kind="test", command="pytest a.py", status="passed")
    ledger.for_session(CHILD_B).append(kind="lint", command="ruff b.py", status="failed")

    out = await _finish_tool().coroutine(session_id=SESSION, flow_id=FLOW)

    assert "failing verification evidence" in out
    assert "ruff b.py" in out


@pytest.mark.asyncio
async def test_the_creators_own_verification_counts_too(
    isolated_db: Path, ledger: EvidenceLedger, monkeypatch: pytest.MonkeyPatch
):
    """Rows under the flow's creator session are part of the flow's evidence."""
    await _seed_flow([_step("s1", "done", child_session_key=CHILD_A)])
    ledger.for_session(CHILD_A).append(kind="test", command="pytest a.py", status="passed")
    ledger.for_session(SESSION).append(kind="lint", command="ruff .", status="failed")

    out = await _finish_tool().coroutine(session_id=SESSION, flow_id=FLOW)

    assert "failing verification evidence" in out
    assert "ruff ." in out


# ---------------------------------------------------------------------------
# Fail-open — an unavailable probe never blocks a finish
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_finish_fail_open_when_evidence_collector_raises(
    isolated_db: Path, ledger: EvidenceLedger, monkeypatch: pytest.MonkeyPatch
):
    await _seed_flow([_step("s1", "done")])
    from agent.tools.taskflow import evidence_collector

    def boom(**_kwargs):
        raise RuntimeError("ledger unavailable")

    monkeypatch.setattr(evidence_collector, "collect_evidence_summary", boom)

    out = await _finish_tool().coroutine(session_id=SESSION, flow_id=FLOW)

    assert "TaskFlow finished" in out
