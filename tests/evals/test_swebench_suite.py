"""SWE-bench suite: quiesce failure and per-instance result plumbing."""

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


@pytest.mark.asyncio
async def test_a_quiesce_timeout_is_a_harness_error(tmp_path, monkeypatch):
    async def _bind(_sid, _repo):
        return None

    async def _drive(_sid, _prompt):
        return {
            "tool_calls": 0,
            "text": "",
            "resumes": 0,
            "interrupted": 0,
            "timeout": False,
            "error": "",
        }

    async def _quiesce(_sid, _root, **_kwargs):
        return {
            "ok": False,
            "timed_out": True,
            "active_descendants": 1,
            "background_running": 0,
            "shadow_writers": 0,
        }

    async def _evidence(_sid):
        return {
            "input": 0,
            "output": 0,
            "cache_read": 0,
            "cost_usd": 0.0,
            "final_text": "",
        }

    monkeypatch.setattr(suite.harness, "_bind_session", _bind)
    monkeypatch.setattr(suite.harness, "_drive", _drive)
    monkeypatch.setattr(suite.teardown, "quiesce_session", _quiesce)
    monkeypatch.setattr(suite, "_session_evidence", _evidence)
    monkeypatch.setattr(suite.instances, "diff", lambda _i: "")
    monkeypatch.setattr(suite.instances, "changed_paths", lambda _i: [])
    monkeypatch.setattr(suite.instances, "revert", lambda _i, **_k: None)

    result = await suite._run_instance(_instance(tmp_path), tmp_path)

    assert result["failure_kind"] == "harness"
    assert result["invalid_run"] is True
    assert "quiesce timed out" in result["error"]


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
