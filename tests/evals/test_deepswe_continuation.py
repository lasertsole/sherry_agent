"""The eval driver's plan-continuation reader (the server owns the real trigger)."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from evals.deepswe import suite

pytestmark = [pytest.mark.unit, pytest.mark.timeout(30)]


def test_open_plan_items_reads_open_todos_and_flows(tmp_path: Path):
    stores = tmp_path / "stores"
    stores.mkdir()
    with sqlite3.connect(stores / "registry.db") as connection:
        connection.execute(
            "CREATE TABLE todos (todo_id INTEGER PRIMARY KEY, content TEXT, status TEXT)"
        )
        connection.execute("CREATE TABLE task_flows (flow_id TEXT PRIMARY KEY, status TEXT)")
        connection.execute("INSERT INTO todos VALUES (1, 'finished item', 'completed')")
        connection.execute("INSERT INTO todos VALUES (2, 'verify item', 'in_progress')")
        connection.execute("INSERT INTO todos VALUES (3, 'skipped item', 'cancelled')")
        connection.execute("INSERT INTO task_flows VALUES ('flow-open', 'running')")
        connection.execute("INSERT INTO task_flows VALUES ('flow-done', 'done')")
        connection.commit()

    items = suite._open_plan_items(tmp_path)

    assert any("verify item" in item and "in_progress" in item for item in items)
    assert any("flow-open" in item for item in items)
    assert not any("finished item" in item for item in items)
    assert not any("flow-done" in item for item in items)


def test_open_plan_items_without_a_registry_is_empty(tmp_path: Path):
    assert suite._open_plan_items(tmp_path) == []


def test_open_plan_items_prefers_the_sandbox_registry(tmp_path: Path):
    sandbox_stores = tmp_path / "sandbox" / "stores"
    sandbox_stores.mkdir(parents=True)
    with sqlite3.connect(sandbox_stores / "registry.db") as connection:
        connection.execute(
            "CREATE TABLE todos (todo_id INTEGER PRIMARY KEY, content TEXT, status TEXT)"
        )
        connection.execute("CREATE TABLE task_flows (flow_id TEXT PRIMARY KEY, status TEXT)")
        connection.execute("INSERT INTO todos VALUES (1, 'sandbox item', 'pending')")
        connection.commit()

    items = suite._open_plan_items(tmp_path)

    assert any("sandbox item" in item for item in items)


def test_acceptance_checklist_extracts_the_normative_clauses():
    instruction = (
        "httpx responses cannot currently stream JSON values.\n"
        "Add Response.iter_json(). These must raise httpx.DecodingError unless "
        "the Content-Type is application/json. Media type matching is "
        "case-insensitive. The +json suffix applies only to application/ types.\n"
    )

    items = suite._acceptance_checklist(instruction)

    joined = " ".join(items)
    assert "must raise" in joined
    assert "case-insensitive" in joined
    assert "only" in joined
    assert all(len(item) >= 20 for item in items)
    assert len(items) <= 8


def test_a_reviewer_spawn_gets_the_checklist_exactly_once(monkeypatch):
    from evals import external_bench

    monkeypatch.setattr(
        external_bench, "_REVIEWER_CHECKLIST", ("item one", "item two"), raising=False
    )
    args = {"task": "verify the change", "functional_role": "reviewer"}

    assert external_bench._augment_reviewer_task(args) is True
    assert "ACCEPTANCE CHECKLIST" in args["task"]
    assert "item one" in args["task"] and "item two" in args["task"]
    assert external_bench._augment_reviewer_task(args) is False


def test_the_task_work_root_stays_outside_the_product_checkout(monkeypatch):
    results = suite.REPO_ROOT / "evals" / "results" / "deepswe" / "run-x"

    default_root = suite._work_root(results)
    assert not str(default_root).startswith(str(suite.REPO_ROOT))

    monkeypatch.setenv("SHERRY_EVAL_WORK_ROOT", str(suite.REPO_ROOT / "_outside"))
    overridden = suite._work_root(results)
    assert overridden == suite.REPO_ROOT / "_outside" / "run-x"
