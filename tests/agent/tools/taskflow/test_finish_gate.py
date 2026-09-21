"""Finish-gate tests: DAG completeness, evidence, and SisyphusVerifier gates.

The finish path is gated before the DONE transition:

* Gate A — every step is done or blocked (none ready/dispatched);
* Gate B — no step is blocked;
* Gate C — flow-scoped evidence has no failing/stale rows;
* Gate D — SisyphusVerifier, only when the caller passes ``todo`` + ``plan_path``.

The tests exercise both the pass and reject branches, the omitted-params path
(Gate D silently skipped), and the fail-open contract (an unavailable evidence
collector or verifier never blocks a finish).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agent.tools.taskflow import build_taskflow_tools
from agent.tools.taskflow.registry import store_sqlite
from agent.tools.todolist.evidence_ledger import EvidenceLedger
from agent.tools.todolist.verifier import SisyphusVerifier

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
        {"description": "finish gate probe", "steps": steps, "results": []},
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
    assert "not done/blocked" in out and "s2" in out


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


@pytest.mark.asyncio
async def test_finish_succeeds_when_evidence_passes(isolated_db: Path, ledger: EvidenceLedger):
    await _seed_flow([_step("s1", "done")])
    ledger.for_session(FLOW).append(kind="test", command="pytest -q", status="passed")

    out = await _finish_tool().coroutine(session_id=SESSION, flow_id=FLOW)

    assert "TaskFlow finished" in out


@pytest.mark.asyncio
async def test_finish_rejected_when_evidence_failed(isolated_db: Path, ledger: EvidenceLedger):
    await _seed_flow([_step("s1", "done")])
    ledger.for_session(FLOW).append(kind="test", command="pytest -q", status="failed")

    out = await _finish_tool().coroutine(session_id=SESSION, flow_id=FLOW)

    assert "failing verification evidence" in out


@pytest.mark.asyncio
async def test_finish_rejected_when_evidence_stale(isolated_db: Path, ledger: EvidenceLedger):
    await _seed_flow([_step("s1", "done")])
    ledger.for_session(FLOW).append(kind="test", command="pytest foo.py", status="passed")
    ledger.for_session(FLOW).mark_stale_for_path("foo.py")

    out = await _finish_tool().coroutine(session_id=SESSION, flow_id=FLOW)

    assert "stale evidence" in out


# ---------------------------------------------------------------------------
# Gate D — SisyphusVerifier (only with explicit todo + plan_path)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_finish_rejected_when_verifier_fails(
    isolated_db: Path, ledger: EvidenceLedger, monkeypatch: pytest.MonkeyPatch
):
    await _seed_flow([_step("s1", "done")])
    captured: dict = {}

    async def fake_verify(*, session_id, todo, plan_path, checkbox_label):
        captured.update(
            session_id=session_id,
            todo=todo,
            plan_path=plan_path,
            checkbox_label=checkbox_label,
        )
        return False, {"reason": "acceptance criteria not found"}

    monkeypatch.setattr(SisyphusVerifier, "verify", fake_verify)

    out = await _finish_tool().coroutine(
        session_id=SESSION,
        flow_id=FLOW,
        todo={"flow_id": FLOW, "step_id": "s1"},
        plan_path="plans/p.md",
        checkbox_label="wire the gate",
    )

    assert "SisyphusVerifier" in out and "acceptance criteria not found" in out
    assert captured["checkbox_label"] == "wire the gate"
    assert captured["todo"] == {"flow_id": FLOW, "step_id": "s1"}


@pytest.mark.asyncio
async def test_finish_succeeds_when_verifier_passes(
    isolated_db: Path, ledger: EvidenceLedger, monkeypatch: pytest.MonkeyPatch
):
    await _seed_flow([_step("s1", "done")])

    async def fake_verify(**_kwargs):
        return True, {}

    monkeypatch.setattr(SisyphusVerifier, "verify", fake_verify)

    out = await _finish_tool().coroutine(
        session_id=SESSION,
        flow_id=FLOW,
        todo={"flow_id": FLOW, "step_id": "s1"},
        plan_path="plans/p.md",
    )

    assert "TaskFlow finished" in out


@pytest.mark.asyncio
async def test_finish_skips_verifier_when_params_omitted(
    isolated_db: Path, ledger: EvidenceLedger, monkeypatch: pytest.MonkeyPatch
):
    await _seed_flow([_step("s1", "done")])

    async def must_not_run(**_kwargs):
        raise AssertionError("verifier must be skipped without todo + plan_path")

    monkeypatch.setattr(SisyphusVerifier, "verify", must_not_run)

    out = await _finish_tool().coroutine(session_id=SESSION, flow_id=FLOW)

    assert "TaskFlow finished" in out


@pytest.mark.asyncio
async def test_finish_skips_verifier_when_only_todo_passed(
    isolated_db: Path, ledger: EvidenceLedger, monkeypatch: pytest.MonkeyPatch
):
    await _seed_flow([_step("s1", "done")])

    async def must_not_run(**_kwargs):
        raise AssertionError("verifier requires both todo and plan_path")

    monkeypatch.setattr(SisyphusVerifier, "verify", must_not_run)

    out = await _finish_tool().coroutine(
        session_id=SESSION, flow_id=FLOW, todo={"flow_id": FLOW, "step_id": "s1"}
    )

    assert "TaskFlow finished" in out


# ---------------------------------------------------------------------------
# Fail-open — an unavailable probe never blocks a finish
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_finish_fail_open_when_verifier_raises(
    isolated_db: Path, ledger: EvidenceLedger, monkeypatch: pytest.MonkeyPatch
):
    await _seed_flow([_step("s1", "done")])

    async def boom(**_kwargs):
        raise RuntimeError("verifier exploded")

    monkeypatch.setattr(SisyphusVerifier, "verify", boom)

    out = await _finish_tool().coroutine(
        session_id=SESSION,
        flow_id=FLOW,
        todo={"flow_id": FLOW, "step_id": "s1"},
        plan_path="plans/p.md",
        checkbox_label="x",
    )

    assert "TaskFlow finished" in out


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
