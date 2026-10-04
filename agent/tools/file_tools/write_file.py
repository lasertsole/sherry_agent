"""write file tool with project root restriction and autopep8 formatting for .py files.

Concurrency contract: the main agent and every subagent share one process and
one tool instance, so two writers can target the same file at once. Every write
runs under ``file_write_lock`` (in-process per-path lock, then the cross-process
``flock``) and lands through ``atomic_write_text_no_follow`` — a reader sees the
old content or the new content, never a torn file, and a crash mid-write cannot
truncate the target.

Read-before-write contract: overwriting an existing file additionally requires
that THIS session has read it — a complete ``read_file``, a previous whole-file
write, or a patch/app rendered against a read — tracked in
``agent/tools/pub_base/read_state.py``. The license's revision is passed to the
atomic write as ``expected_revision``, so an edit that landed after the read is
REFUSED ("re-read and retry") instead of silently destroyed; a file this
session never read is refused outright. ``append`` does not overwrite, so it
stays licensed-free."""

import asyncio
import contextlib
import json
import os
import stat
from pathlib import Path
from typing import Annotated, Literal, override

from pydantic import BaseModel

from langchain_core.callbacks import CallbackManagerForToolRun
from runtime.session.project_dir import current_project_dir
from langchain_community.tools.file_management import WriteFileTool
from langchain_community.tools.file_management.write import WriteFileInput
from langchain_core.tools import InjectedToolCallId
from langgraph.prebuilt.tool_node import InjectedState

from agent.tools.pub_base import (
    FileBusyError,
    FileLicense,
    PathOutOfBoundsError,
    StaleWriteError,
    _extract_session_id,
    _open_no_follow,
    atomic_write_bytes_no_follow,
    decode_text,
    display_path,
    encode_text,
    file_revision,
    file_write_lock,
    licensed,
    note_edit,
    note_overwrite,
    read_bytes_no_follow,
    resolve_external_path,
    resolve_workspace_path,
    safe_error_detail,
    sniff_text_encoding,
)
from agent.tools.todolist.evidence_recorder import mark_evidence_stale
from .snapshot import capture_pre_write, finalize_capture

SessionId = Annotated[str, InjectedState("session_id")]
ToolCallId = Annotated[str, InjectedToolCallId]


class FormattedWriteFileInput(WriteFileInput):
    """WriteFileInput plus the runtime-injected session id (not LLM-facing)."""

    session_id: SessionId = ""
    tool_call_id: ToolCallId = ""


def _format_py_code(text: str) -> str:
    """Format Python code using autopep8 if the content looks like valid Python."""
    try:
        import autopep8

        formatted = autopep8.fix_code(text)
        return formatted
    except Exception:
        return text


class UnreadFileError(Exception):
    """An overwrite of a file this session has no read license for."""

    def __init__(self, path: Path) -> None:
        super().__init__(f"File not read in this session: {path}")
        self.path = path


def _overwrite_precondition(session_id: str, resolved: Path) -> FileLicense | None:
    """The license an overwrite is checked and encoded against.

    ``None`` means "no precondition": the write attempt answers with its own
    error. Only REGULAR files are gated — a symlinked final component is refused
    by the write itself (``ELOOP``, the established policy) and a directory
    fails with its own error, so "read it first" would be nonsense for either.
    A path that does not exist is licensed as ``absent`` (and encoded as
    UTF-8), so a create is refused by the atomic write when another writer
    creates the file first — the same gate a stale license hits.
    :raises UnreadFileError: The file exists and this session never read it.
    """
    try:
        info = resolved.lstat()
    except FileNotFoundError:
        return FileLicense(revision="absent")
    except OSError:
        return None  # unreadable metadata: let the write attempt answer
    if not stat.S_ISREG(info.st_mode):
        return None
    license = licensed(session_id, resolved)
    if license is None:
        raise UnreadFileError(resolved)
    return license


def _append_bytes_no_follow(resolved: Path, data: bytes) -> None:
    """Append through ``_open_no_follow`` so a symlink final component is refused."""
    flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND
    fd = _open_no_follow(resolved, flags, 0o644)
    try:
        with os.fdopen(fd, "wb") as f:
            fd = -1
            f.write(data)
    finally:
        if fd >= 0:
            os.close(fd)


def _existing_text_encoding(resolved: Path) -> str | None | Literal["absent"]:
    """Encoding of the existing target: a codec, ``None`` for binary, ``"absent"``.

    ``None`` (binary or not UTF-8/UTF-16) is the caller's refusal signal: the
    text tools never write into a file they cannot read back, because that is
    how a resource file gets replaced with mojibake.
    """
    try:
        data, _stat = read_bytes_no_follow(resolved)
    except FileNotFoundError:
        return "absent"
    return sniff_text_encoding(data)


class FormattedWriteFileTool(WriteFileTool):
    """WriteFileTool that auto-formats .py files with autopep8."""

    args_schema: type[BaseModel] = FormattedWriteFileInput
    description: str = (
        "Write file to disk. Creating a file needs nothing else; overwriting a file "
        "that already exists requires having read it in this session first "
        "(read_file) — otherwise the call is refused. Set append=true to add to an "
        "existing file without overwriting it."
    )

    # ── shared core ────────────────────────────────────────────────────────

    def _core(
        self,
        file_path: str,
        text: str,
        append: bool = False,
        session_id: str = "",
        tool_call_id: str = "",
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
                existing_encoding = _existing_text_encoding(resolved)
                if existing_encoding is None:
                    # A resource file (image, archive, UTF-16/legacy text): the
                    # text tools refuse rather than replace it with mojibake.
                    return json.dumps(
                        {
                            "error": (
                                "Target is a binary or non-UTF-8 file "
                                "(only UTF-8 and BOM-marked UTF-16 text is edited)."
                            ),
                            "path": display_path(resolved, root),
                            "hint": (
                                "Use terminal (e.g. cp / python) for resource files; "
                                "write_file edits text only."
                            ),
                        },
                        ensure_ascii=False,
                    )
                if append and existing_encoding not in ("utf-8", "utf-8-sig"):
                    # Appending into UTF-16 would either write UTF-8 bytes into a
                    # UTF-16 file or plant a second BOM mid-file. A whole-file
                    # read + write is the safe path, and the license allows it.
                    return json.dumps(
                        {
                            "error": (
                                "Cannot append to a non-UTF-8 text file "
                                f"(detected {existing_encoding}); append is UTF-8 only."
                            ),
                            "path": display_path(resolved, root),
                            "hint": "Read the file and write the whole content instead of appending.",
                        },
                        ensure_ascii=False,
                    )
                if is_py and append:
                    # Append then format the WHOLE file: one atomic write of the
                    # reformatted result instead of the old append+rewrite pair.
                    existing = ""
                    with contextlib.suppress(FileNotFoundError):
                        existing = decode_text(read_bytes_no_follow(resolved)[0], existing_encoding)
                    capture = capture_pre_write(resolved, root)
                    written = encode_text(_format_py_code(existing + text), existing_encoding)
                    atomic_write_bytes_no_follow(resolved, written)
                    note_edit(session_id, resolved, file_revision(resolved))
                    finalize_capture(
                        capture, session_id=session_id, tool_call_id=tool_call_id, written=written
                    )
                elif append:
                    # Plain append keeps O_APPEND semantics (crash-tolerant by
                    # construction); the lock still serializes its writers. The
                    # bytes are UTF-8 — a BOM (utf-8-sig) belongs to the file's
                    # first bytes only and is never re-emitted here.
                    capture = capture_pre_write(resolved, root)
                    chunk = text.encode("utf-8")
                    _append_bytes_no_follow(resolved, chunk)
                    note_edit(session_id, resolved, file_revision(resolved))
                    finalize_capture(
                        capture,
                        session_id=session_id,
                        tool_call_id=tool_call_id,
                        written=(capture.before_bytes or b"") + chunk if capture else chunk,
                    )
                else:
                    try:
                        license = _overwrite_precondition(session_id, resolved)
                    except UnreadFileError:
                        return json.dumps(
                            {
                                "error": (
                                    "File exists but has not been read in this session; "
                                    "refusing to overwrite content the agent has not seen."
                                ),
                                "path": display_path(resolved, root),
                                "hint": (
                                    "Read it with read_file first, then write — or use "
                                    "patch_file for a targeted change."
                                ),
                            },
                            ensure_ascii=False,
                        )
                    assert license is not None  # the gate above answered otherwise
                    payload = _format_py_code(text) if is_py else text
                    written = encode_text(payload, license.encoding)
                    capture = capture_pre_write(resolved, root)
                    # ``absent`` is a real precondition: a file another writer
                    # creates in the window is refused, not clobbered.
                    atomic_write_bytes_no_follow(
                        resolved,
                        written,
                        expected_revision=license.revision,
                    )
                    # This session authored the whole file: it knows the revision
                    # it produced, so the next overwrite is licensed against it
                    # — and in the codec it was written in.
                    note_overwrite(
                        session_id,
                        resolved,
                        file_revision(resolved),
                        encoding=license.encoding,
                    )
                    finalize_capture(
                        capture, session_id=session_id, tool_call_id=tool_call_id, written=written
                    )
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
        tool_call_id: str = "",
        run_manager: CallbackManagerForToolRun | None = None,
    ) -> str:
        session_id = session_id or _extract_session_id(run_manager)
        return self._core(file_path, text, append, session_id, tool_call_id)

    @override
    async def _arun(
        self,
        file_path: str,
        text: str,
        append: bool = False,
        session_id: str = "",
        tool_call_id: str = "",
        run_manager: CallbackManagerForToolRun | None = None,
    ) -> str:
        session_id = session_id or _extract_session_id(run_manager)
        return await asyncio.to_thread(
            self._core, file_path, text, append, session_id, tool_call_id
        )


def build_write_file_tool() -> WriteFileTool:
    tool = FormattedWriteFileTool(
        root_dir="/"  # containment is enforced by resolve_project_path / resolve_external_path
    )
    tool.handle_tool_error = True
    tool.name = "write_file"
    tool.metadata = {"idempotent": False}
    return tool
