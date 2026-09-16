"""write file tool with project root restriction and autopep8 formatting for .py files."""

import asyncio
import json
import os
from pathlib import Path
from typing import Annotated, override

from pydantic import BaseModel

from langchain_core.callbacks import CallbackManagerForToolRun
from langchain_community.tools.file_management import WriteFileTool
from langchain_community.tools.file_management.write import WriteFileInput
from langgraph.prebuilt.tool_node import InjectedState

from agent.tools.pub_base import (
    PathOutOfBoundsError,
    _extract_session_id,
    _open_no_follow,
    display_path,
    resolve_external_path,
    resolve_project_path,
    safe_error_detail,
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


def _write_text_no_follow(resolved: Path, text: str, append: bool) -> None:
    """Write text through ``_open_no_follow`` so a symlink final component is refused."""
    flags = os.O_WRONLY | os.O_CREAT | (os.O_APPEND if append else os.O_TRUNC)
    fd = _open_no_follow(resolved, flags, 0o644)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as f:
            fd = -1
            f.write(text)
    finally:
        if fd >= 0:
            os.close(fd)


def _read_text_no_follow(resolved: Path) -> str:
    """Read text through ``_open_no_follow`` (used by the .py append+format flow)."""
    fd = _open_no_follow(resolved, os.O_RDONLY)
    try:
        with os.fdopen(fd, "r", encoding="utf-8") as f:
            fd = -1
            return f.read()
    finally:
        if fd >= 0:
            os.close(fd)


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

        try:
            resolved.parent.mkdir(parents=True, exist_ok=True)
            is_py = resolved.suffix == ".py"
            if is_py and append:
                # Append first, then format the entire file
                _write_text_no_follow(resolved, text, append=True)
                formatted = _format_py_code(_read_text_no_follow(resolved))
                _write_text_no_follow(resolved, formatted, append=False)
            else:
                _write_text_no_follow(resolved, _format_py_code(text) if is_py else text, append)
        except Exception as e:
            return "Error: " + safe_error_detail(e)

        return f"File written successfully to {display_path(resolved)}."

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
