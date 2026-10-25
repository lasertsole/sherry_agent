"""Official AgentBench DBBench task data and environment materialisation.

The suite uses AgentBench's own test file (``data/dbbench/standard.jsonl``, 300
questions with labelled answers) from a pinned checkout of
https://github.com/THUDM/AgentBench
(commit d1e4a10db08c87075c78972e48ecc182be03e2d5):
https://github.com/THUDM/AgentBench/blob/d1e4a10db08c87075c78972e48ecc182be03e2d5/data/dbbench/standard.jsonl
Nothing here generates questions, answers or tables: every task is one official
row, and the label is the row's own ``label`` field.

Environment adaptation (documented in the report):

* AgentBench's official DBBench environment builds a **MySQL 8** database from
  the row's ``table.table_info`` and exposes ``execute_sql`` /
  ``commit_final_answer`` functions. This suite materialises the SAME official
  table (columns + rows) into SQLite and exposes the same two function names;
  the agent therefore runs against official data, but SQL dialect differences
  remain (SQLite instead of MySQL).
* Tasks whose rows cannot be represented that way (multi-table rows, or
  INSERT/DELETE/UPDATE tasks whose official verdict is a table hash) are
  reported UNSUPPORTED with the reason instead of being approximated.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from evals import external_bench

AGENTBENCH_REPO = "https://github.com/THUDM/AgentBench.git"
#: The commit this suite's loader/grader were written against.
AGENTBENCH_PIN = "d1e4a10db08c87075c78972e48ecc182be03e2d5"
DATA_SUBDIR = "data/dbbench"
DATA_FILE = "standard.jsonl"

#: The official standard.jsonl is 300 tasks; the default arm is its first ten
#: indices (an index slice of the official test set, never a new task set).
DEFAULT_SLICE = tuple(range(10))

_WRITE_TYPES = frozenset({"INSERT", "DELETE", "UPDATE"})


@dataclass(frozen=True)
class Task:
    """One official DBBench row."""

    index: int
    task_id: str
    question: str
    evidence: str
    add_description: str
    table_name: str
    columns: tuple[str, ...]
    rows: tuple[tuple[Any, ...], ...]
    label: tuple[str, ...]
    query_type: str
    source: str
    supported: bool = True
    unsupported_reason: str = ""


def fetch_tasks() -> tuple[Path, str]:
    """Return the official ``data/dbbench`` directory and the checkout commit."""
    return external_bench.local_or_cloned(
        "SHERRY_AGENTBENCH_REPO",
        external_bench.CACHE_ROOT / "agentbench",
        AGENTBENCH_REPO,
        AGENTBENCH_PIN,
        label="AgentBench",
        subdir=DATA_SUBDIR,
    )


def _columns(table_info: dict[str, Any]) -> tuple[str, ...]:
    columns = table_info.get("columns") or []
    return tuple(str(column.get("name") or "").strip() for column in columns if column.get("name"))


def _rows(table_info: dict[str, Any]) -> tuple[tuple[Any, ...], ...]:
    rows = table_info.get("rows") or []
    return tuple(tuple(row) for row in rows if isinstance(row, list))


def _label(entry: dict[str, Any]) -> tuple[str, ...]:
    raw = entry.get("label")
    if raw is None:
        return ()
    if isinstance(raw, str):
        return (raw,)
    return tuple(str(item) for item in raw)


def _unsupported_reason(entry: dict[str, Any], table: Any) -> str:
    query_type = str((entry.get("type") or [""])[0]).upper()
    if query_type in _WRITE_TYPES:
        return f"{query_type} task needs the official MySQL table-hash verdict"
    if not isinstance(table, dict):
        return "multi-table task (the SQLite arm represents one table)"
    table_info = table.get("table_info")
    if not isinstance(table_info, dict):
        return "row carries no table_info"
    if not _rows(table_info):
        return "table_info carries no rows"
    if not _columns(table_info):
        return "table_info carries no columns"
    if not _label(entry):
        return "row carries no label"
    return ""


def load_tasks(root: Path) -> dict[str, Task]:
    """Load the official standard.jsonl into ``{task_id: Task}``."""
    path = root / DATA_FILE
    if not path.is_file():
        raise RuntimeError(f"AgentBench data file not found: {path}")
    tasks: dict[str, Task] = {}
    for index, line in enumerate(path.read_text(encoding="utf-8").splitlines()):
        line = line.strip()
        if not line:
            continue
        entry = json.loads(line)
        table = entry.get("table")
        reason = _unsupported_reason(entry, table)
        table_info = table.get("table_info") if isinstance(table, dict) else {}
        table_info = table_info if isinstance(table_info, dict) else {}
        query_type = str((entry.get("type") or [""])[0]).upper()
        task_id = f"dbbench-{index:03d}"
        tasks[task_id] = Task(
            index=index,
            task_id=task_id,
            question=str(entry.get("description") or ""),
            evidence=str(entry.get("evidence") or ""),
            add_description=str(entry.get("add_description") or ""),
            table_name=str(table.get("table_name") or "") if isinstance(table, dict) else "",
            columns=_columns(table_info),
            rows=_rows(table_info),
            label=_label(entry),
            query_type=query_type or "SELECT",
            source=str(entry.get("source") or ""),
            supported=not reason,
            unsupported_reason=reason,
        )
    return tasks


def select(tasks: dict[str, Task], ids: tuple[str, ...]) -> list[Task]:
    """Resolve task ids (official slice order) and refuse unknown ids."""
    selected: list[Task] = []
    missing: list[str] = []
    for task_id in ids:
        task = tasks.get(task_id)
        if task is None:
            missing.append(task_id)
        else:
            selected.append(task)
    if missing:
        raise KeyError(f"unknown AgentBench DBBench task id(s): {missing}")
    return selected


def _quote_ident(name: str) -> str:
    """Quote one SQLite identifier."""
    return '"' + name.replace('"', '""') + '"'


def materialize_sqlite(task: Task, path: Path) -> None:
    """Create the official table in SQLite from its official columns + rows."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.unlink(missing_ok=True)
    conn = sqlite3.connect(path)
    try:
        quoted = ", ".join(f"{_quote_ident(name)} TEXT" for name in task.columns)
        table = _quote_ident(task.table_name)
        conn.execute(f"CREATE TABLE {table} ({quoted})")
        placeholders = ", ".join("?" for _ in task.columns)
        conn.executemany(
            f"INSERT INTO {table} VALUES ({placeholders})",
            [row[: len(task.columns)] for row in task.rows],
        )
        conn.commit()
    finally:
        conn.close()


def task_prompt(task: Task) -> str:
    """The official DBBench system + user prompt for one task."""
    from evals.agentbench.grading import OFFICIAL_SYSTEM_PROMPT

    user = ""
    if task.evidence:
        user += "Evidence about the question: " + task.evidence + "\n"
    if task.add_description:
        user += "Additional table information about the question: " + task.add_description + "\n"
    user += "Question: " + task.question + "\n"
    return (
        f"{OFFICIAL_SYSTEM_PROMPT}\n\n"
        f"{user}\n"
        "Use `execute_sql` to run one SQL statement at a time (the database is "
        "SQLite). When you are sure of the answer, call `commit_final_answer` "
        "with the list of answer strings — that tool is the ONLY accepted way "
        "to submit."
    )
