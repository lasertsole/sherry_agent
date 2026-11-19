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
