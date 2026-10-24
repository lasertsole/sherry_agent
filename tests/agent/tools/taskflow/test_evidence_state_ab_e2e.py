"""A/B e2e: real write_file producer → Gate C, with and without B' snapshots.

A's evidence command is ``pytest test_beta.py`` and never names the source file
``alpha.py``. With the old append-only evidence row, B overwriting alpha.py does
not invalidate A; with B', A's verification snapshots alpha.py into
``modified_files`` and the cross-session broadcast marks A stale.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agent.tools.file_tools.read_file import build_read_file_tool
from agent.tools.file_tools.write_file import build_write_file_tool
from agent.tools.pub_base.evidence_ledger import EvidenceLedger
from agent.tools.pub_base.evidence_recorder import record_verification_evidence
from agent.tools.taskflow import build_taskflow_tools
from agent.tools.taskflow.evidence_collector import collect_evidence_summary
from agent.tools.taskflow.registry import store_sqlite
from runtime.session.state_keys import StateKey
from runtime.session.state_register import state_register_mem

pytestmark = [pytest.mark.integration]

SESSION = "sess-state-ab"
FLOW = "flow-state-ab"
CHILD_A = "agent:main:subagent:state-ab-a"
CHILD_B = "agent:main:subagent:state-ab-b"
CREATOR = f"agent:main:session:{SESSION}"
COMMAND = "python -m pytest test_beta.py -q"


@pytest.fixture()
def project(tmp_path: Path) -> Path:
    root = tmp_path / "project"
    root.mkdir()
    (root / "alpha.py").write_text("def value():\n    return 1\n", encoding="utf-8")
    (root / "test_beta.py").write_text(
        "from alpha import value\n\n\ndef test_value():\n    assert value() == 1\n",
        encoding="utf-8",
    )
    for key in (CHILD_A, CHILD_B, CREATOR):
        state_register_mem.set_state(key, StateKey.PROJECT_DIR, str(root))
    yield root
    for key in (CHILD_A, CHILD_B, CREATOR):
        state_register_mem.clear_session(key)


@pytest.fixture()
def ledger(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> EvidenceLedger:
    monkeypatch.setattr(EvidenceLedger, "LEDGER_PATH", str(tmp_path / "evidence.db"))
    return EvidenceLedger


def _finish_tool():
    return {tool.name: tool for tool in build_taskflow_tools()}["taskflow_finish"]


async def _seed_flow() -> None:
    await store_sqlite.create_flow(
        FLOW,
        {
            "description": "evidence state A/B",
            "steps": [
                {
                    "step_id": "a",
                    "task": "verify alpha",
                    "depends_on": [],
                    "status": "done",
                    "child_session_key": CHILD_A,
                }
            ],
            "results": [],
            "creator_session_key": CREATOR,
        },
        session_id=SESSION,
    )


def _write_alpha(session: str, value: int) -> None:
    text = f"def value():\n    return {value}\n"
    build_read_file_tool()._run("alpha.py", session_id=session)
    result = build_write_file_tool()._run("alpha.py", text, session_id=session)
    assert "successfully" in result or "success" in result, result


@pytest.mark.asyncio
async def test_without_a_snapshot_the_cross_file_write_is_invisible(
    isolated_db: Path, ledger: EvidenceLedger, project: Path
):
    await _seed_flow()
    _write_alpha(CHILD_A, 1)
    # Simulate a pre-B' evidence row: no modified_files snapshot.
    EvidenceLedger.for_session(CHILD_A).append(kind="test", command=COMMAND, status="passed")
    _write_alpha(CHILD_B, 2)

    out = await _finish_tool().coroutine(session_id=SESSION, flow_id=FLOW)

    assert "TaskFlow finished" in out, out
    assert "[stale]" not in (collect_evidence_summary(session_key=CHILD_A) or "")


@pytest.mark.asyncio
async def test_the_snapshot_makes_gate_c_reject_the_cross_file_write(
    isolated_db: Path, ledger: EvidenceLedger, project: Path
):
    await _seed_flow()
    _write_alpha(CHILD_A, 1)
    record_verification_evidence(COMMAND, "1 passed", CHILD_A)
    _write_alpha(CHILD_B, 2)

    out = await _finish_tool().coroutine(session_id=SESSION, flow_id=FLOW)

    assert out.startswith("Error: Finish gate rejected: stale evidence"), out
    assert "[stale]" in out
    summary = collect_evidence_summary(session_key=CHILD_A) or ""
    assert "[stale]" in summary
