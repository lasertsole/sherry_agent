"""A/B: declared `file_deps` vs no declaration, and prioritize vs filter.

All cases use a real temporary project and real ``python -m pytest`` replays.
The declared step's test imports ``alpha.py``; extra steps have their own
passing tests so the replay cap/order is observable.

- No declaration + a tight cap: the broken command sits after the cap and is
  never replayed -> the flow finishes (miss).
- Declaration + the same cap: the affected step is replayed first -> broken.
- ``filter`` mode replays only declared sessions (cheaper) but misses an
  undeclared step's break; ``prioritize`` (default) replays the rest too.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from agent.tools.pub_base.evidence_ledger import EvidenceLedger
from agent.tools.pub_base.evidence_recorder import mark_evidence_stale
from agent.tools.pub_base.regression_gate import prioritized_sessions
from agent.tools.taskflow import build_legacy_taskflow_tools
from agent.tools.taskflow.registry import store_sqlite
from config.features import EVIDENCE_REGRESSION_GATE
from runtime.session.state_keys import StateKey
from runtime.session.state_register import state_register_mem

pytestmark = [pytest.mark.integration]

SESSION = "sess-file-deps-ab"
FLOW = "flow-file-deps-ab"
CREATOR = f"agent:main:session:{SESSION}"
CHILD_U1 = "agent:main:subagent:file-deps-u1"
CHILD_U2 = "agent:main:subagent:file-deps-u2"
CHILD_U3 = "agent:main:subagent:file-deps-u3"
CHILD_A = "agent:main:subagent:file-deps-a"
CHILD_C = "agent:main:subagent:file-deps-c"
CHILD_B = "agent:main:subagent:file-deps-b"

U_COMMANDS = [
    f"{sys.executable} -m pytest u{index}_test.py -q -p no:cacheprovider" for index in (1, 2, 3)
]
A_COMMAND = f"{sys.executable} -m pytest test_beta.py -q -p no:cacheprovider"
C_COMMAND = f"{sys.executable} -m pytest test_gamma.py -q -p no:cacheprovider"


@pytest.fixture()
def project(tmp_path: Path) -> Path:
    root = tmp_path / "project"
    root.mkdir()
    (root / "alpha.py").write_text("def value():\n    return 1\n", encoding="utf-8")
    (root / "delta.py").write_text("def value():\n    return 1\n", encoding="utf-8")
    (root / "test_beta.py").write_text(
        "from alpha import value\n\n\ndef test_value():\n    assert value() == 1\n",
        encoding="utf-8",
    )
    (root / "test_gamma.py").write_text(
        "from delta import value\n\n\ndef test_value():\n    assert value() == 1\n",
        encoding="utf-8",
    )
    for index in (1, 2, 3):
        (root / f"u{index}_test.py").write_text(
            f"def test_unrelated_{index}():\n    assert True\n",
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
    monkeypatch.setitem(EVIDENCE_REGRESSION_GATE, "file_deps_mode", "prioritize")
    return EvidenceLedger


def _finish_tool():
    return {tool.name: tool for tool in build_legacy_taskflow_tools()}["taskflow_finish"]


async def _seed_flow(
    *,
    file_deps: list[str] | None = None,
    include_gamma: bool = False,
) -> None:
    steps = [
        {
            "step_id": f"u{index}",
            "task": f"unrelated {index}",
            "depends_on": [],
            "status": "done",
            "child_session_key": child,
        }
        for index, child in enumerate((CHILD_U1, CHILD_U2, CHILD_U3), start=1)
    ]
    a_step = {
        "step_id": "a",
        "task": "verify alpha",
        "depends_on": [],
        "status": "done",
        "child_session_key": CHILD_A,
    }
    if file_deps is not None:
        a_step["file_deps"] = file_deps
    steps.append(a_step)
    if include_gamma:
        steps.append(
            {
                "step_id": "c",
                "task": "verify delta",
                "depends_on": [],
                "status": "done",
                "child_session_key": CHILD_C,
            }
        )
    steps.append(
        {
            "step_id": "b",
            "task": "write sources",
            "depends_on": [],
            "status": "done",
            "child_session_key": CHILD_B,
        }
    )
    await store_sqlite.create_flow(
        FLOW,
        {
            "description": "file_deps A/B",
            "steps": steps,
            "results": [],
            "creator_session_key": CREATOR,
        },
        session_id=SESSION,
    )


def _evidence(include_gamma: bool = False) -> None:
    for child, command in zip((CHILD_U1, CHILD_U2, CHILD_U3), U_COMMANDS, strict=True):
        EvidenceLedger.for_session(child).append(kind="test", command=command, status="passed")
    EvidenceLedger.for_session(CHILD_A).append(kind="test", command=A_COMMAND, status="passed")
    if include_gamma:
        EvidenceLedger.for_session(CHILD_C).append(kind="test", command=C_COMMAND, status="passed")


def _break_alpha(project: Path) -> None:
    (project / "alpha.py").write_text("def value():\n    return 2\n", encoding="utf-8")


def _break_delta(project: Path) -> None:
    (project / "delta.py").write_text("def value():\n    return 2\n", encoding="utf-8")


def _steps_with_deps(file_deps: list[str] | None) -> list[dict]:
    step: dict = {"step_id": "a", "child_session_key": CHILD_A}
    if file_deps is not None:
        step["file_deps"] = file_deps
    return [step]


@pytest.mark.asyncio
async def test_without_file_deps_a_capped_replay_misses_the_break(
    isolated_db: Path,
    ledger: EvidenceLedger,
    project: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setitem(EVIDENCE_REGRESSION_GATE, "max_reverify_commands", 1)
    await _seed_flow()
    _evidence()
    _break_alpha(project)
    mark_evidence_stale("alpha.py", CHILD_B)

    out = await _finish_tool().coroutine(session_id=SESSION, flow_id=FLOW)

    assert "TaskFlow finished" in out, out


@pytest.mark.asyncio
async def test_a_declared_dependency_is_replayed_first_and_catches_the_break(
    isolated_db: Path,
    ledger: EvidenceLedger,
    project: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setitem(EVIDENCE_REGRESSION_GATE, "max_reverify_commands", 1)
    await _seed_flow(file_deps=["alpha.py"])
    _evidence()
    _break_alpha(project)
    mark_evidence_stale("alpha.py", CHILD_B)

    keys = [CHILD_U1, CHILD_U2, CHILD_U3, CHILD_A, CHILD_B, CREATOR]
    assert prioritized_sessions(
        _steps_with_deps(["alpha.py"]), keys, creator_session_key=CREATOR
    ) == [CHILD_A]

    out = await _finish_tool().coroutine(session_id=SESSION, flow_id=FLOW)

    assert out.startswith("Error: Finish gate rejected: confirmed regression"), out
    assert "[broken]" in out


@pytest.mark.asyncio
async def test_prioritize_catches_an_undeclared_steps_break(
    isolated_db: Path,
    ledger: EvidenceLedger,
    project: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setitem(EVIDENCE_REGRESSION_GATE, "file_deps_mode", "prioritize")
    await _seed_flow(file_deps=["alpha.py"], include_gamma=True)
    _evidence(include_gamma=True)
    mark_evidence_stale("alpha.py", CHILD_B)
    _break_delta(project)
    mark_evidence_stale("delta.py", CHILD_B)

    out = await _finish_tool().coroutine(session_id=SESSION, flow_id=FLOW)

    assert out.startswith("Error: Finish gate rejected: confirmed regression"), out
    assert "[broken]" in out


@pytest.mark.asyncio
async def test_filter_is_cheaper_but_misses_an_undeclared_steps_break(
    isolated_db: Path,
    ledger: EvidenceLedger,
    project: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setitem(EVIDENCE_REGRESSION_GATE, "file_deps_mode", "filter")
    await _seed_flow(file_deps=["alpha.py"], include_gamma=True)
    _evidence(include_gamma=True)
    mark_evidence_stale("alpha.py", CHILD_B)
    _break_delta(project)
    mark_evidence_stale("delta.py", CHILD_B)

    out = await _finish_tool().coroutine(session_id=SESSION, flow_id=FLOW)

    # Only the declared step is replayed; the undeclared step's break is missed.
    assert "TaskFlow finished" in out, out
