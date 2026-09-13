"""write file tool with project root restriction and autopep8 formatting for .py files."""

import asyncio
import json
from typing import Annotated, override

from pydantic import BaseModel

from langchain_core.callbacks import CallbackManagerForToolRun
from langchain_community.tools.file_management import WriteFileTool
from langchain_community.tools.file_management.write import WriteFileInput
from langgraph.prebuilt.tool_node import InjectedState

from agent.tools.pub_base import (
    PathOutOfBoundsError,
    _extract_session_id,
    resolve_external_path,
    resolve_project_path,
)

SessionId = Annotated[str, InjectedState("session_id")]


class FormattedWriteFileInput(WriteFileInput):
    """WriteFileInput plus the runtime-injected session id (not LLM-facing)."""

    session_id: SessionId = ""


def _format_py_code(text: str) -> str:
    """Format Python code using autopep8 if the content looks like valid Python."""
    try:
        import autopep8

        formatted = autopep8.fix_code(text)
        return formatted
    except Exception:
        return text


class FormattedWriteFileTool(WriteFileTool):
    """WriteFileTool that auto-formats .py files with autopep8."""

    args_schema: type[BaseModel] = FormattedWriteFileInput

    # ── shared core ────────────────────────────────────────────────────────

    def _core(
        self,
        file_path: str,
        text: str,
        append: bool = False,
        session_id: str = "",
    ) -> str:
        try:
            resolved = resolve_project_path(file_path)
        except PathOutOfBoundsError:
            try:
                resolved = resolve_external_path(
                    file_path, session_id=session_id, action_desc="write file"
                )
            except PathOutOfBoundsError as e:
                return json.dumps({"error": str(e)}, ensure_ascii=False)

        is_py = resolved.suffix == ".py"
        if is_py:
            if append:
                # Append first, then format the entire file
                result = super()._run(file_path=str(resolved), text=text, append=True)
                full_text = resolved.read_text(encoding="utf-8")
                formatted = _format_py_code(full_text)
                super()._run(file_path=str(resolved), text=formatted, append=False)
                return result
            else:
                # New write: format the content upfront
                text = _format_py_code(text)
                return super()._run(file_path=str(resolved), text=text, append=False)

        return super()._run(file_path=str(resolved), text=text, append=append)

    @override
    def _run(
        self,
        file_path: str,
        text: str,
        append: bool = False,
        session_id: str = "",
        run_manager: CallbackManagerForToolRun | None = None,
    ) -> str:
        session_id = session_id or _extract_session_id(run_manager)
        return self._core(file_path, text, append, session_id)

    @override
    async def _arun(
        self,
        file_path: str,
        text: str,
        append: bool = False,
        session_id: str = "",
        run_manager: CallbackManagerForToolRun | None = None,
    ) -> str:
        session_id = session_id or _extract_session_id(run_manager)
        return await asyncio.to_thread(self._core, file_path, text, append, session_id)


def build_write_file_tool() -> WriteFileTool:
    tool = FormattedWriteFileTool(
        root_dir="/"  # containment is enforced by resolve_project_path / resolve_external_path
    )
    tool.handle_tool_error = True
    tool.name = "write_file"
    tool.metadata = {"idempotent": False}
    return tool
