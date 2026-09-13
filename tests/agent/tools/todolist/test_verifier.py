"""Unit tests for the Sisyphus verifier (E5, plan todo 8).

The verifier implements the evidence-recording half of the 5-gate completion
contract. Every external read (plan file, TaskFlow flow, subagent run, ledger)
is a module-level injectable reference on ``agent.tools.todolist.verifier``, so
tests substitute fakes and never touch real TaskFlow/subagent state or the real
``.omo/ledger.jsonl``.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from agent.tools.todolist import verifier
from agent.tools.todolist.evidence_ledger import EvidenceLedger
from agent.tools.todolist.verifier import SisyphusVerifier

pytestmark = [pytest.mark.unit]

LABEL = "8. Add verifier"


def _write_plan(
    tmp_path: Path,
    label: str = LABEL,
    *,
    checked: bool = False,
    verification: str = "uv run pytest -q",
) -> Path:
    """Write a minimal plan file with one labelled checkbox + indented criteria."""
    box = "x" if checked else " "
    path = tmp_path / "plan.md"
    path.write_text(
        "# Plan\n\n## Todos\n\n"
        f"- [{box}] {label}\n"
        "  - Recommended task executor category: quick\n"
        f"  - Verification: `{verification}`\n"
        "  - Files in scope: agent/tools/todolist/verifier.py\n"
        "- [ ] 9. next task\n  - Verification: `echo next`\n",
        encoding="utf-8",
    )
    return path


@pytest.fixture(autouse=True)
def _isolated_ledger(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect the ledger into tmp_path for every test in this module."""
    ledger = tmp_path / "ledger.jsonl"
    monkeypatch.setattr(verifier.EvidenceLedger, "LEDGER_PATH", str(ledger))
    return ledger


def test_extract_acceptance_criteria_returns_indented_lines(tmp_path: Path):
    content = verifier._read_plan(str(_write_plan(tmp_path)))

    acceptance = verifier._extract_acceptance_criteria(content, LABEL)

    assert acceptance is not None
    assert "Verification:" in acceptance
    assert "Files in scope" in acceptance
    assert "9. next task" not in acceptance


def test_extract_acceptance_criteria_matches_checked_checkbox(tmp_path: Path):
    content = verifier._read_plan(str(_write_plan(tmp_path, checked=True)))

    acceptance = verifier._extract_acceptance_criteria(content, LABEL)

    assert acceptance is not None


def test_extract_acceptance_criteria_none_for_unknown_label(tmp_path: Path):
    content = verifier._read_plan(str(_write_plan(tmp_path)))

    assert verifier._extract_acceptance_criteria(content, "no such checkbox") is None


def test_extract_verification_commands_strips_backticks():
    acceptance = (
        "- Verification: `uv run pytest tests/agent/tools/todolist -q`\n- Files in scope: a.py"
    )

    assert verifier._extract_verification_commands(acceptance) == [
        "uv run pytest tests/agent/tools/todolist -q"
    ]


def test_verify_passes_and_records_commands_and_cleanup(tmp_path: Path):
    plan = _write_plan(tmp_path)

    passed, evidence = asyncio.run(SisyphusVerifier.verify("sess-1", {}, str(plan), LABEL))

    assert passed is True
    assert evidence["event"] == "task-completed"
    assert evidence["plan"] == str(plan)
    assert evidence["task"] == LABEL
    assert evidence["session_id"] == "sess-1"
    assert evidence["commands"] == ["uv run pytest -q"]
    assert evidence["cleanup"] == []
    assert evidence["adversarial_classes"]["plan_reread"] == "passed"


def test_verify_appends_one_entry_to_tmp_ledger(tmp_path: Path):
    plan = _write_plan(tmp_path)

    passed, _evidence = asyncio.run(SisyphusVerifier.verify("sess-ledger", {}, str(plan), LABEL))

    assert passed is True
    entries = EvidenceLedger.read_all()
    assert len(entries) == 1
    assert entries[0]["event"] == "task-completed"
    assert entries[0]["task"] == LABEL


def test_verify_fails_when_label_missing(tmp_path: Path):
    plan = _write_plan(tmp_path)

    passed, evidence = asyncio.run(SisyphusVerifier.verify("sess-1", {}, str(plan), "absent label"))

    assert passed is False
    assert evidence["reason"]
    assert EvidenceLedger.read_all() == []


def test_verify_fails_when_plan_missing(tmp_path: Path):
    missing = tmp_path / "does-not-exist.md"

    passed, evidence = asyncio.run(SisyphusVerifier.verify("sess-1", {}, str(missing), LABEL))

    assert passed is False
    assert evidence["reason"]
    assert EvidenceLedger.read_all() == []


def test_verify_fails_when_linked_step_not_done(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    plan = _write_plan(tmp_path)

    async def _flow(flow_id: str) -> dict:
        return {"state": {"steps": [{"step_id": "step-2", "status": "dispatched"}]}}

    monkeypatch.setattr(verifier, "_load_flow", _flow)

    passed, evidence = asyncio.run(
        SisyphusVerifier.verify(
            "sess-1", {"flow_id": "flow-1", "step_id": "step-2"}, str(plan), LABEL
        )
    )

    assert passed is False
    assert "dispatched" in evidence["reason"]
    assert EvidenceLedger.read_all() == []


def test_verify_passes_when_linked_step_done(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    plan = _write_plan(tmp_path)

    async def _flow(flow_id: str) -> dict:
        return {"state": {"steps": [{"step_id": "step-2", "status": "done"}]}}

    monkeypatch.setattr(verifier, "_load_flow", _flow)

    passed, evidence = asyncio.run(
        SisyphusVerifier.verify(
            "sess-1", {"flow_id": "flow-1", "step_id": "step-2"}, str(plan), LABEL
        )
    )

    assert passed is True
    assert evidence["adversarial_classes"]["taskflow_step"] == "done"


def test_verify_never_raises_when_flow_missing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    plan = _write_plan(tmp_path)

    async def _missing(flow_id: str) -> None:
        return None

    monkeypatch.setattr(verifier, "_load_flow", _missing)

    passed, evidence = asyncio.run(
        SisyphusVerifier.verify(
            "sess-1", {"flow_id": "ghost", "step_id": "step-1"}, str(plan), LABEL
        )
    )

    assert passed is False
    assert evidence["reason"]
    assert EvidenceLedger.read_all() == []


def test_verify_fails_when_subagent_still_live(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    plan = _write_plan(tmp_path)

    monkeypatch.setattr(verifier, "_get_run_by_child_session_key", lambda key: "run-record")
    monkeypatch.setattr(verifier, "_is_live_unended_run", lambda run: True)

    passed, evidence = asyncio.run(
        SisyphusVerifier.verify("sess-1", {"subagent_id": "child-1"}, str(plan), LABEL)
    )

    assert passed is False
    assert evidence["reason"]
    assert EvidenceLedger.read_all() == []


def test_verify_passes_when_subagent_run_ended(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    plan = _write_plan(tmp_path)

    monkeypatch.setattr(verifier, "_get_run_by_child_session_key", lambda key: "run-record")
    monkeypatch.setattr(verifier, "_is_live_unended_run", lambda run: False)

    passed, _evidence = asyncio.run(
        SisyphusVerifier.verify("sess-1", {"subagent_id": "child-1"}, str(plan), LABEL)
    )

    assert passed is True
