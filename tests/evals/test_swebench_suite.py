"""SWE-bench suite: no-edit repair, quiesce failure, incremental results."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from evals.swebench import suite

pytestmark = [pytest.mark.unit]


def _instance(tmp_path: Path):
    return SimpleNamespace(
        instance_id="sympy__sympy-0001",
        repo="sympy/sympy",
        base_commit="0" * 40,
        problem_statement="fix it",
        test_patch="",
        gold_patch="",
        fail_to_pass=["t"],
        pass_to_pass=[],
        repo_dir=tmp_path / "repo",
        python_exe=Path("/usr/bin/python3"),
        version="",
        node_ids=lambda: (["t"], []),
    )


@pytest.fixture()
def _fake_run(monkeypatch):
    calls = {"drive": 0, "quiesce": 0, "revert": 0}
    drives = [
        {
            "tool_calls": 3,
            "text": "first",
            "resumes": 0,
            "interrupted": 0,
            "timeout": False,
            "error": "",
        },
        {
            "tool_calls": 2,
            "text": "second",
            "resumes": 0,
            "interrupted": 0,
            "timeout": False,
            "error": "",
        },
    ]
    changes = [[], ["fix.py"]]

    async def _bind(_sid, _repo):
        return None

    async def _drive(_sid, _prompt):
        index = min(calls["drive"], len(drives) - 1)
        calls["drive"] += 1
        return dict(drives[index])

    async def _quiesce(_sid, _root, **_kwargs):
        calls["quiesce"] += 1
        return {
            "ok": True,
            "timed_out": False,
            "active_descendants": 0,
            "background_running": 0,
            "shadow_writers": 0,
        }

    async def _evidence(_sid):
        return {
            "input": 100,
            "output": 10,
            "cache_read": 80,
            "cost_usd": 0.001,
            "final_text": "final",
        }

    def _changed(_instance):
        index = min(calls["drive"] - 1, len(changes) - 1)
        return list(changes[max(0, index)])

    monkeypatch.setattr(suite.harness, "_bind_session", _bind)
    monkeypatch.setattr(suite.harness, "_drive", _drive)
    monkeypatch.setattr(suite.teardown, "quiesce_session", _quiesce)
    monkeypatch.setattr(suite, "_session_evidence", _evidence)
    monkeypatch.setattr(suite.instances, "diff", lambda _i: "diff")
    monkeypatch.setattr(suite.instances, "changed_paths", _changed)
    monkeypatch.setattr(suite.instances, "apply_patch", lambda _i, _p: True)
    monkeypatch.setattr(suite.instances, "run_tests", lambda _i, _n: (True, "ok", []))
    monkeypatch.setattr(
        suite.instances,
        "revert",
        lambda _i, **_k: calls.__setitem__("revert", calls["revert"] + 1),
    )
    return calls


@pytest.mark.asyncio
async def test_a_no_edit_turn_gets_one_repair_turn(tmp_path, _fake_run):
    result = await suite._run_instance(_instance(tmp_path), tmp_path)

    assert _fake_run["drive"] == 2
    assert result["resolved"] is True
    assert result["changed_paths"] == ["fix.py"]
    assert result["tool_calls"] == 5
    assert result["quiesce"]["ok"] is True
    assert result["tokens"]["cache_read"] == 80
    assert result["cost_usd"] == 0.001
    assert _fake_run["revert"] == 1


@pytest.mark.asyncio
async def test_a_repair_that_still_makes_no_change_is_no_edit(tmp_path, monkeypatch, _fake_run):
    monkeypatch.setattr(suite.instances, "changed_paths", lambda _i: [])
    monkeypatch.setattr(suite.instances, "run_tests", lambda _i, _n: (False, "fail", []))

    result = await suite._run_instance(_instance(tmp_path), tmp_path)

    assert _fake_run["drive"] == 2
    assert result["failure_kind"] == "no_edit"
    assert result["invalid_run"] is False


@pytest.mark.asyncio
async def test_a_quiesce_timeout_is_a_harness_error(tmp_path, monkeypatch, _fake_run):
    async def _timeout(_sid, _root, **_kwargs):
        return {
            "ok": False,
            "timed_out": True,
            "active_descendants": 1,
            "background_running": 0,
            "shadow_writers": 0,
        }

    monkeypatch.setattr(suite.teardown, "quiesce_session", _timeout)

    result = await suite._run_instance(_instance(tmp_path), tmp_path)

    assert result["failure_kind"] == "harness"
    assert result["invalid_run"] is True
    assert "quiesce timed out" in result["error"]


def test_merge_drive_sums_counters_and_keeps_errors():
    merged = suite._merge_drive(
        {
            "tool_calls": 1,
            "text": "a",
            "resumes": 1,
            "interrupted": 0,
            "timeout": False,
            "error": "",
        },
        {
            "tool_calls": 2,
            "text": "b",
            "resumes": 0,
            "interrupted": 1,
            "timeout": True,
            "error": "x",
        },
    )

    assert merged == {
        "tool_calls": 3,
        "text": "ab",
        "resumes": 1,
        "interrupted": 1,
        "timeout": True,
        "error": "x",
    }


def test_harness_error_result_and_incremental_write(tmp_path):
    result = suite._harness_error_result(_instance(tmp_path), RuntimeError("boom"))

    assert result["failure_kind"] == "harness"
    assert result["invalid_run"] is True
    suite._write_instance_result(tmp_path, result)
    written = json.loads(
        (tmp_path / "instance_results" / "sympy__sympy-0001.json").read_text(encoding="utf-8")
    )
    assert written["id"] == "sympy__sympy-0001"
    assert "boom" in written["error"]
