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
    monkeypatch.setattr(suite.instances, "diff", lambda _i, **_k: "")
    monkeypatch.setattr(suite.instances, "changed_paths", lambda _i, **_k: [])
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


def test_tool_result_stats_separates_empty_evicted_and_missing():
    from langchain_core.messages import ToolMessage

    from pub.func.transcript_repair import EMPTY_TOOL_RESULT, MISSING_TOOL_RESULT

    messages = [
        ToolMessage(content=EMPTY_TOOL_RESULT, tool_call_id="1"),
        ToolMessage(content="[evicted to: /x/evicted/terminal-a.log]", tool_call_id="2"),
        ToolMessage(content=MISSING_TOOL_RESULT, tool_call_id="3"),
        ToolMessage(
            content="[Duplicated call to terminal - output cleared, see latest result]",
            tool_call_id="4",
        ),
        ToolMessage(content="[Old tool result content cleared]", tool_call_id="5"),
        ToolMessage(
            content="[...truncated by context compression, tool result expired (TTL=300s)...]",
            tool_call_id="6",
        ),
        ToolMessage(
            content="[Output was truncated due to eviction threshold. Use read_file...]",
            tool_call_id="7",
        ),
    ]

    stats = suite._tool_result_stats(messages)

    assert stats == {
        "empty": 1,
        "evicted": 1,
        "missing": 1,
        "deduped": 1,
        "pruned": 1,
        "ttl": 1,
        "sliced": 1,
    }


def test_target_tests_block_exposes_the_patch_and_f2p_names():
    instance = SimpleNamespace(
        test_patch="diff --git a/tests/test_x.py b/tests/test_x.py\n+assert x\n"
    )

    block = suite._target_tests_block(
        instance, ["tests/test_x.py::test_x"], ["tests/test_x.py::test_old"]
    )

    assert "Evaluation tests (read-only)" in block
    assert "FAIL_TO_PASS" in block
    assert "tests/test_x.py::test_x" in block
    assert "PASS_TO_PASS (must keep passing)" in block
    assert "tests/test_x.py::test_old" in block
    assert "diff --git a/tests/test_x.py" in block


def test_target_tests_block_is_empty_without_a_patch():
    instance = SimpleNamespace(test_patch="")

    assert suite._target_tests_block(instance, ["t"]) == ""


def test_target_tests_block_truncates_a_large_patch():
    instance = SimpleNamespace(test_patch="x" * (suite._TEST_EXPOSURE_MAX_CHARS + 100))

    block = suite._target_tests_block(instance, ["t"])

    assert "test patch truncated" in block
    assert "x" * (suite._TEST_EXPOSURE_MAX_CHARS + 1) not in block


def test_target_tests_block_marks_preapplied_tests():
    instance = SimpleNamespace(test_patch="diff --git a/tests/test_x.py b/tests/test_x.py\n")

    block = suite._target_tests_block(instance, ["t"], preapplied=True)

    assert "ALREADY APPLIED" in block


def test_the_dirty_diff_gate_recognises_test_paths():
    assert suite._looks_like_test("tests/bulk_create/test_dbg.py") is True
    assert suite._looks_like_test("pkg/tests/test_x.py") is True
    assert suite._looks_like_test("test_dbg.py") is True
    assert suite._looks_like_test("django/db/models/query.py") is False
    assert suite._looks_like_test("fix_compiler.py") is False


def test_task_template_requires_reading_the_target_tests_before_editing():
    assert "Before editing, read the" in suite._TASK_TEMPLATE
    assert "FAIL_TO_PASS and PASS_TO_PASS" in suite._TASK_TEMPLATE
    assert "derive the" in suite._TASK_TEMPLATE
