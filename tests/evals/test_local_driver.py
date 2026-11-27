"""Regression: the local driver cannot report a staged artifact as a result.

Measured 2026-11-27: the pest verifier could not build anything (cargo
``rc=102`` — no network) and copied a TWO-DAY-OLD junit file into
``base1..4.xml`` / ``new.xml``; the grader's "missing == failed" turned it into
``F2P 104/104, P2P 0/250`` and the driver reported it as a normal miss. The same
run also graded a cancelled turn (``Request cancelled``) as a completed one, and
computed compliance from gate keys that do not exist.

These tests pin the three rules: an artifact's OWN timestamp must be from this
run; a cancelled turn is a distinct outcome; compliance comes from the tool
frames (as the container report does).
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import pytest

from evals.deepswe import local_driver

pytestmark = [pytest.mark.unit, pytest.mark.timeout(30)]

_STALE = datetime.fromisoformat("2026-11-25T22:08:37.633+08:00").timestamp()
_NOW = datetime.fromisoformat("2026-11-27T19:46:00+08:00").timestamp()


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_compliance_uses_the_tool_frame_counts() -> None:
    assert local_driver.compliance_from_tool_names({"todowrite": 1, "taskflow_flow": 1})
    assert not local_driver.compliance_from_tool_names({"todowrite": 1})
    # Round 1 looked for these names on the GATE bucket and always answered False.
    assert not local_driver.compliance_from_tool_names({"todo": 1, "plan_calls": 1})


def test_a_stale_junit_artifact_invalidates_the_run(tmp_path: Path) -> None:
    out = tmp_path / "verifier"
    for name in ("base1.xml", "new.xml"):
        _write(
            out / "task" / name,
            '<testsuites timestamp="2026-11-25T22:08:37.633+08:00" tests="104"/>',
        )

    assert local_driver.stale_verifier_artifacts(out, _NOW) == ["task/base1.xml", "task/new.xml"]


def test_a_fresh_ctrf_report_is_not_stale(tmp_path: Path) -> None:
    out = tmp_path / "verifier"
    now_ms = int(_NOW * 1000)
    _write(
        out / "task" / "base-ctrf.json",
        json.dumps({"results": {"summary": {"start": now_ms, "stop": now_ms + 1000}}}),
    )

    assert local_driver.stale_verifier_artifacts(out, _NOW) == []


def test_an_artifact_without_a_timestamp_is_unknown_not_stale(tmp_path: Path) -> None:
    out = tmp_path / "verifier"
    _write(out / "task" / "new.xml", '<testsuites name="vitest tests" tests="19" failures="14"/>')

    assert local_driver.stale_verifier_artifacts(out, _NOW) == []


def test_a_stale_ctrf_report_is_stale(tmp_path: Path) -> None:
    out = tmp_path / "verifier"
    old_ms = int(_STALE * 1000)
    _write(
        out / "task" / "base-ctrf.json",
        json.dumps({"results": {"summary": {"start": old_ms, "stop": old_ms}}}),
    )

    assert local_driver.stale_verifier_artifacts(out, _NOW) == ["task/base-ctrf.json"]


def test_cancelled_and_provider_outcomes_are_distinct() -> None:
    base = {"timeout": False, "cancelled": False, "error": ""}

    assert local_driver.drive_outcome({**base, "cancelled": True}) == "cancelled"
    assert (
        local_driver.drive_outcome(
            {**base, "error": "APIStatusError: Error code: 402 - insufficient_balance"}
        )
        == "provider"
    )
    assert local_driver.drive_outcome({**base, "timeout": True}) == "timeout"
    assert local_driver.drive_outcome(base) == ""


def test_sandbox_policy_prefers_the_explicit_choice(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SHERRY_LOCAL_EVAL_SANDBOX_POLICY", "off")
    assert local_driver.sandbox_policy() == "off"

    monkeypatch.delenv("SHERRY_LOCAL_EVAL_SANDBOX_POLICY")
    monkeypatch.setattr(local_driver.shutil, "which", lambda name: None)
    assert local_driver.sandbox_policy() == "off"

    monkeypatch.setattr(local_driver.shutil, "which", lambda name: "/usr/bin/bwrap")
    assert local_driver.sandbox_policy() == "auto"


def _task_result(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "id": "task",
        "resolved": False,
        "failure_kind": "",
        "invalid_run": False,
        "orchestration_compliant": True,
        "plan_open": [],
        "error": "",
    }
    base.update(overrides)
    return base


def test_an_unfinished_plan_is_flow_incomplete_and_invalid() -> None:
    result = _task_result(plan_open=["flow: shorthand-flow (running)"])

    local_driver._apply_invalid_rules(result)

    assert result["failure_kind"] == "flow_incomplete"
    assert result["invalid_run"] is True
    assert result["orchestration_compliant"] is None


def test_a_resolved_task_keeps_its_verdict() -> None:
    result = _task_result(resolved=True, plan_open=["todo: leftover"])

    local_driver._apply_invalid_rules(result)

    assert result["failure_kind"] == ""
    assert result["invalid_run"] is False
    assert result["orchestration_compliant"] is True


def test_cancelled_and_stale_verifier_are_invalid() -> None:
    cancelled = _task_result(failure_kind="cancelled")
    stale = _task_result(failure_kind="stale_verifier")
    plain_miss = _task_result()

    for result in (cancelled, stale, plain_miss):
        local_driver._apply_invalid_rules(result)

    assert cancelled["invalid_run"] is True
    assert stale["invalid_run"] is True
    assert plain_miss["invalid_run"] is False
