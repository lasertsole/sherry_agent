"""write file tool with project root restriction and autopep8 formatting for .py files.

Concurrency contract: the main agent and every subagent share one process and
one tool instance, so two writers can target the same file at once. Every write
runs under ``file_write_lock`` (in-process per-path lock, then the cross-process
``flock``) and lands through ``atomic_write_text_no_follow`` — a reader sees the
old content or the new content, never a torn file, and a crash mid-write cannot
truncate the target. This tool does NOT read first (a blind overwrite has no
revision to check), so its conflicts are serialized rather than refused; a busy
cross-process lock answers with an actionable error instead of writing anyway."""

import asyncio
import contextlib
import json
import os
from pathlib import Path
from typing import Annotated, override

from pydantic import BaseModel

from langchain_core.callbacks import CallbackManagerForToolRun
from runtime.session.project_dir import current_project_dir
from langchain_community.tools.file_management import WriteFileTool
from langchain_community.tools.file_management.write import WriteFileInput
from langgraph.prebuilt.tool_node import InjectedState

from agent.tools.pub_base import (
    FileBusyError,
    PathOutOfBoundsError,
    StaleWriteError,
    _extract_session_id,
    _open_no_follow,
    atomic_write_text_no_follow,
    display_path,
    file_write_lock,
    read_bytes_no_follow,
    resolve_external_path,
    resolve_workspace_path,
    safe_error_detail,
)
from agent.tools.todolist.evidence_recorder import mark_evidence_stale

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
        # redundant: path_guard middleware handles this — kept as the second line of defense
        try:
            root = current_project_dir(session_id)
            resolved = resolve_workspace_path(file_path, root)
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
            # One writer per path at a time: in-process (other agents) and
            # cross-process (a second Sherry process on the same project root).
            with file_write_lock(resolved):
                if is_py and append:
                    # Append then format the WHOLE file: one atomic write of the
                    # reformatted result instead of the old append+rewrite pair.
                    existing = ""
                    with contextlib.suppress(FileNotFoundError):
                        existing = read_bytes_no_follow(resolved)[0].decode("utf-8")
                    atomic_write_text_no_follow(resolved, _format_py_code(existing + text))
                elif append:
                    # Plain append keeps O_APPEND semantics (crash-tolerant by
                    # construction); the lock still serializes its writers.
                    _write_text_no_follow(resolved, text, append=True)
                else:
                    atomic_write_text_no_follow(resolved, _format_py_code(text) if is_py else text)
        except StaleWriteError:
            return json.dumps(
                {
                    "error": "File changed on disk since it was read.",
                    "path": display_path(resolved, root),
                    "hint": "Re-read the file and retry the write.",
                },
                ensure_ascii=False,
            )
        except FileBusyError as e:
            return json.dumps(
                {
                    "error": str(e),
                    "path": display_path(resolved, root),
                    "hint": "Another process is writing this file; retry shortly.",
                },
                ensure_ascii=False,
            )
        except Exception as e:
            return "Error: " + safe_error_detail(e)

        mark_evidence_stale(file_path, session_id)
        return f"File written successfully to {display_path(resolved, root)}."

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
