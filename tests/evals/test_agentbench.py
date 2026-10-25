"""Hermetic checks for the AgentBench DBBench arm (no network, no model)."""

from __future__ import annotations

import json

import pytest

from evals.agentbench import grading, instances
from evals.agentbench.instances import Task

pytestmark = [pytest.mark.unit]


def _task(**overrides) -> Task:
    values = {
        "index": 0,
        "task_id": "dbbench-test",
        "question": "q",
        "evidence": "",
        "add_description": 'The table is "T" with columns A, B.',
        "table_name": "T",
        "columns": ("A", "B"),
        "rows": (("x", "1"), ("y", "2")),
        "label": ("2",),
        "query_type": "OTHER",
        "source": "test",
    }
    values.update(overrides)
    return Task(**values)


def test_official_comparison_rules():
    assert grading.compare_results(["2"], ["2"], "OTHER")
    assert grading.compare_results(["2.0"], ["2"], "OTHER")  # float tolerance
    assert grading.compare_results(["a", "b"], ["b", "a"], "OTHER")  # set comparison
    assert not grading.compare_results(["a"], ["b"], "OTHER")
    assert grading.compare_results(["0"], [None], "OTHER")  # special-value mapping
    assert grading.compare_results(["50%"], ["50"], "OTHER")


def test_materialize_sqlite_roundtrip(tmp_path):
    path = tmp_path / "db" / "task.sqlite"
    instances.materialize_sqlite(_task(table_name='T "quoted"'), path)

    import sqlite3

    conn = sqlite3.connect(path)
    try:
        count = conn.execute('SELECT COUNT(*) FROM "T ""quoted"""').fetchone()[0]
        rows = conn.execute('SELECT * FROM "T ""quoted""" ORDER BY A').fetchall()
    finally:
        conn.close()
    assert count == 2
    assert rows[0] == ("x", "1")


def test_the_two_official_tools_execute_and_commit(tmp_path, monkeypatch):
    import agent.core as agent_core

    from evals.agentbench import tools

    task = _task()
    db_path = tmp_path / "task.sqlite"
    instances.materialize_sqlite(task, db_path)
    monkeypatch.setattr(agent_core, "_tools", [])
    state = tools.install_dbbench_tools(db_path)
    by_name = {tool.name: tool for tool in agent_core.get_agent_tools()}

    assert set(by_name) == {"execute_sql", "commit_final_answer"}
    assert by_name["execute_sql"]._run("SELECT B FROM T WHERE A = 'y'") == '[["2"]]'
    assert by_name["commit_final_answer"]._run(["2"]) == "Answer committed."
    assert state.final_answers == ["2"]


def test_load_tasks_marks_write_rows_unsupported(tmp_path):
    read_row = {
        "description": "q",
        "label": ["x"],
        "type": ["other"],
        "table": {
            "table_name": "T",
            "table_info": {"columns": [{"name": "A"}], "rows": [["x"]]},
        },
    }
    write_row = {
        "description": "q",
        "type": ["INSERT"],
        "table": {
            "table_name": "T",
            "table_info": {"columns": [{"name": "A"}], "rows": [["x"]]},
        },
        "answer_md5": "deadbeef",
    }
    (tmp_path / instances.DATA_FILE).write_text(
        json.dumps(read_row) + "\n" + json.dumps(write_row) + "\n", encoding="utf-8"
    )

    tasks = instances.load_tasks(tmp_path)

    assert tasks["dbbench-000"].supported
    assert not tasks["dbbench-001"].supported
    assert "table-hash" in tasks["dbbench-001"].unsupported_reason


def test_prompt_carries_the_official_system_text():
    prompt = instances.task_prompt(_task())

    assert prompt.startswith(grading.OFFICIAL_SYSTEM_PROMPT)
    assert "Additional table information" in prompt
    assert "Question: q" in prompt


def test_agentbench_is_registered():
    from evals.evals import SUITES

    assert SUITES["agentbench"] == "evals.agentbench.suite"
