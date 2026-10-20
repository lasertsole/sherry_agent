"""The two official DBBench tools, bound to one task's SQLite database.

AgentBench's DBBench environment gives the model exactly two functions:
``execute_sql`` and ``commit_final_answer``. This module installs the same two
names on the eval agent; their descriptions mirror the official
``configs/tasks/dbbench.yaml`` text. The database is the official table
materialised by ``instances.materialize_sqlite``.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from langchain_core.tools import BaseTool
from pydantic import BaseModel, ConfigDict, Field

_EXECUTE_DESCRIPTION = "Executes a given SQL statement on the database and returns the result."
_COMMIT_DESCRIPTION = "Commits the final answer after all operations are completed."

_MAX_RESULT_CHARS = 8_000


@dataclass
class DBBenchEvalState:
    """Observations collected for grading and the report."""

    final_answers: list[str] | None = None
    sql_calls: list[dict[str, Any]] = field(default_factory=list)


class _ExecuteSQLInput(BaseModel):
    query: str = Field(description="The SQL query to be executed.")


class _CommitAnswerInput(BaseModel):
    answers: list[str] = Field(description="The list of final answers to commit.")


class ExecuteSQLTool(BaseTool):
    """``execute_sql`` — read-only SQL over the official task table."""

    name: str = "execute_sql"
    description: str = _EXECUTE_DESCRIPTION
    args_schema: Any = _ExecuteSQLInput
    db_path: Any = Field(exclude=True)
    state: Any = Field(exclude=True)
    model_config = ConfigDict(arbitrary_types_allowed=True)

    def _run(self, query: str, **_: Any) -> str:
        self.state.sql_calls.append({"query": query})
        try:
            # Read-only URI: the official SELECT tasks never mutate the table,
            # and a stray write must not corrupt the run's fixture.
            uri = f"file:{self.db_path}?mode=ro"
            with sqlite3.connect(uri, uri=True) as conn:
                cursor = conn.execute(query)
                rows = cursor.fetchall() if cursor.description is not None else []
                text = json.dumps(rows, ensure_ascii=False, default=str)
                return text[:_MAX_RESULT_CHARS]
        except Exception as exc:  # noqa: BLE001 - SQL errors are observations
            return f"SQL error: {type(exc).__name__}: {exc}"

    async def _arun(self, query: str, **kwargs: Any) -> str:
        return await asyncio.to_thread(self._run, query, **kwargs)


class CommitFinalAnswerTool(BaseTool):
    """``commit_final_answer`` — records the official answer list."""

    name: str = "commit_final_answer"
    description: str = _COMMIT_DESCRIPTION
    args_schema: Any = _CommitAnswerInput
    state: Any = Field(exclude=True)
    model_config = ConfigDict(arbitrary_types_allowed=True)

    def _run(self, answers: list[str], **_: Any) -> str:
        self.state.final_answers = [str(answer) for answer in answers]
        return "Answer committed."

    async def _arun(self, answers: list[str], **kwargs: Any) -> str:
        return self._run(answers, **kwargs)


def install_dbbench_tools(db_path: Path) -> DBBenchEvalState:
    """Replace/append the two tools on the live agent tool list."""
    import agent.core as agent_core

    state = DBBenchEvalState()
    tools = agent_core.get_agent_tools()
    tools[:] = [
        tool
        for tool in tools
        if getattr(tool, "name", "") not in {"execute_sql", "commit_final_answer"}
    ]
    tools.append(
        ExecuteSQLTool(
            db_path=db_path,
            state=state,
            metadata={"idempotent": False, "side_effect_scope": "system"},
        )
    )
    tools.append(
        CommitFinalAnswerTool(
            state=state,
            metadata={"idempotent": False, "side_effect_scope": "user_interaction"},
        )
    )
    return state
