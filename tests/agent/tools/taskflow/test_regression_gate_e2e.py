"""End-to-end comparison: §2 stale propagation vs the §3 finish-time replay.

A real temporary project, a real ``python -m pytest`` replay, and the real
``taskflow_finish`` tool. The three cases show why the finish-time gate adds
coverage beyond a cross-session stale row:

1. §2 only: a sibling breaks an imported module -> no stale row (the test
   command does not name the source file) -> the flow finishes anyway.
2. §3: the same break -> the replay fails -> ``[broken]`` -> the flow is
   rejected.
3. §3 clears a false positive: a write invalidates the test file's evidence,
   but the test still passes -> ``unmark`` -> the flow finishes.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from agent.tools.pub_base.evidence_ledger import EvidenceLedger
from agent.tools.pub_base.evidence_recorder import mark_evidence_stale
from agent.tools.taskflow import build_legacy_taskflow_tools
from agent.tools.taskflow.registry import store_sqlite
from agent.tools.taskflow.evidence_collector import collect_evidence_summary
from config.features import EVIDENCE_REGRESSION_GATE
from runtime.session.state_keys import StateKey
from runtime.session.state_register import state_register_mem

pytestmark = [pytest.mark.integration]

SESSION = "sess-regression-e2e"
FLOW = "flow-regression-e2e"
CHILD_A = "agent:main:subagent:regression-a"
CHILD_B = "agent:main:subagent:regression-b"
CREATOR = f"agent:main:session:{SESSION}"
COMMAND = f"{sys.executable} -m pytest test_beta.py -q -p no:cacheprovider"


@pytest.fixture()
def project(tmp_path: Path) -> Path:
    root = tmp_path / "project"
    root.mkdir()
    (root / "alpha.py").write_text("def value():\n    return 1\n", encoding="utf-8")
    (root / "test_beta.py").write_text(
        "from alpha import value\n\n\ndef test_value():\n    assert value() == 1\n",
        encoding="utf-8",
    )
    for key in (SESSION, CREATOR):
        state_register_mem.set_state(key, StateKey.PROJECT_DIR, str(root))
    yield root
    for key in (SESSION, CREATOR):
        state_register_mem.clear_session(key)


@pytest.fixture()
def ledger(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> EvidenceLedger:
    monkeypatch.setattr(EvidenceLedger, "LEDGER_PATH", str(tmp_path / "evidence.db"))
    monkeypatch.setitem(EVIDENCE_REGRESSION_GATE, "enabled", True)
    monkeypatch.setitem(EVIDENCE_REGRESSION_GATE, "reverify_timeout_s", 30)
    monkeypatch.setitem(EVIDENCE_REGRESSION_GATE, "max_reverify_commands", 10)
    return EvidenceLedger


def _finish_tool():
    return {tool.name: tool for tool in build_legacy_taskflow_tools()}["taskflow_finish"]


async def _seed_flow() -> None:
    await store_sqlite.create_flow(
        FLOW,
        {
            "description": "regression gate e2e",
            "steps": [
                {
                    "step_id": "s1",
                    "task": "verify alpha",
                    "depends_on": [],
                    "status": "done",
                    "child_session_key": CHILD_A,
                },
                {
                    "step_id": "s2",
                    "task": "write alpha",
                    "depends_on": [],
                    "status": "done",
                    "child_session_key": CHILD_B,
                },
            ],
            "results": [],
            "creator_session_key": CREATOR,
        },
        session_id=SESSION,
    )


def _evidence() -> None:
    EvidenceLedger.for_session(CHILD_A).append(
        kind="test",
        command=COMMAND,
        status="passed",
    )


def _break_alpha(project: Path) -> None:
    (project / "alpha.py").write_text("def value():\n    return 2\n", encoding="utf-8")


@pytest.mark.asyncio
async def test_s2_alone_misses_a_cross_file_break(
    isolated_db: Path,
    ledger: EvidenceLedger,
    project: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    """The baseline: the source module is not named by A's command, so no stale."""
    monkeypatch.setitem(EVIDENCE_REGRESSION_GATE, "enabled", False)
    await _seed_flow()
    _evidence()
    _break_alpha(project)
    mark_evidence_stale("alpha.py", CHILD_B)

    out = await _finish_tool().coroutine(session_id=SESSION, flow_id=FLOW)

    assert "TaskFlow finished" in out, out
    summary = collect_evidence_summary(session_key=CHILD_A) or ""
    assert "[stale]" not in summary and "[broken]" not in summary


@pytest.mark.asyncio
async def test_s3_replays_the_command_and_detects_the_cross_file_break(
    isolated_db: Path,
    ledger: EvidenceLedger,
    project: Path,
):
    await _seed_flow()
    _evidence()
    _break_alpha(project)
    mark_evidence_stale("alpha.py", CHILD_B)

    out = await _finish_tool().coroutine(session_id=SESSION, flow_id=FLOW)

    assert out.startswith("Error: Finish gate rejected: confirmed regression"), out
    assert "[broken]" in out
    summary = collect_evidence_summary(session_key=CHILD_A) or ""
    assert "[broken]" in summary


@pytest.mark.asyncio
async def test_s3_unmarks_a_stale_false_positive(
    isolated_db: Path,
    ledger: EvidenceLedger,
    project: Path,
):
    await _seed_flow()
    _evidence()
    # A write of the SAME (still-passing) test content invalidates A's evidence.
    mark_evidence_stale("test_beta.py", CHILD_B)
    assert "[stale]" in (collect_evidence_summary(session_key=CHILD_A) or "")

    out = await _finish_tool().coroutine(session_id=SESSION, flow_id=FLOW)

    assert "TaskFlow finished" in out, out
    summary = collect_evidence_summary(session_key=CHILD_A) or ""
    assert "[stale]" not in summary and "[broken]" not in summary
    assert EvidenceLedger.read_for_session(CHILD_A)[-1]["event"] == "unmark"
